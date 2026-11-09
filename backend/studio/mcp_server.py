"""MCP server exposing the navigation and data tools to external AI agents (streamable HTTP, mounted at /mcp).

What a client gets for free, without anything in its own prompt: the `instructions` (built from the published model, so the agent rules
travel with them and follow every edit), tool descriptions with documented parameters, read-only annotations, a guide resource and ready prompts.
"""
import inspect
import time

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import access, activity, auth, modelview, tools
from .library import agent as library_agent, scope as library_scope

GUIDE = """# @NAME@: how to use this server

This server is the organisation's memory, in two parts. The **ontology** is its dictionary for data: the business entities, the exact meaning of the
words people use about them ("active", "unpaid", a plan name), how they join, what is sensitive, and the database that implements them. The
**knowledge base** is what the organisation knows from its documents: verified topic pages by subject, each fact backed by a claim with its exact quote.

## Tools
Ontology and data: `get_access` (what may I see) - `search` (find by words or codes) - `describe` (what a word means, or anything by name) -
`join_path` (how entities join) - `plan_query` (everything for one SQL statement) - `query_data` (run it, read-only).
Documents (only if your credential has collections): `library_context` (PRIMARY: whole topic pages for a question) - `library_search` (which topics
exist) - `library_topic` (one page) - `library_topic_claims` (facts with exact quotes) - `library_topic_sources` (documents behind a topic) -
`library_traverse` (related topics) - `library_source_search` / `library_source` (the verified page of each document, for detail) -
`library_vocabulary` - `library_collections`.

## Start
Call `get_access` once. Whatever it does not grant is absent from every answer: say it is withheld, never treat it as empty or zero.

## Read every question in three steps
1. **Understand the words.** People use their own words; the ontology says what they mean here. When a question turns on a business word
   (active, open, unpaid, premium, a product, plan or team name, an abbreviation) and you are not sure of its exact meaning, `describe(word)` gives the
   definition, synonyms and the filter that implements it (a word the model does not know is looked up in the documents). Use `search` when you do
   not know the name. Never assume a meaning. (For a data question you can skip this: `plan_query` resolves the words itself.)
2. **Route.**
   - What the organisation SAYS (a policy, procedure, product, plan, price, offer, eligibility rule, how-to) -> the documents: `library_context`.
   - What the data SHOWS (how many, which records, totals, rankings, a record's details) -> the data: `plan_query`, then `query_data`.
   - What something MEANS or where it lives -> the ontology: `describe`, `search`, `join_path`.
   - BOTH (a rule applied to the data: "which customers can still get a refund?", "how many accounts qualify for the premium offer?"):
     a. `library_context` for the rule and its exact conditions;
     b. the topic's `model` list names the entities it is about; `describe` a condition's words if their data meaning is unclear;
     c. `plan_query` with the question restated with those conditions, then ONE statement with `query_data`;
     d. answer with the rule (cite the topic) and the figure (name the definitions used).
3. **Answer with evidence**: topics by title, entities and columns by name, and what was withheld, pending or synthetic.

## Data questions
Only if `get_access` says data is allowed. Work like an analyst who knows SQL:
1. Map each noun of the question to the MOST SPECIFIC entity (a specific entity rather than a general one that unifies several kinds).
2. Call `plan_query` ONCE with the question and ALL the entities involved. For an overview of one record, use its entity as the grain and put every
   related entity in that same call. It returns tables, columns with exact allowed values, join keys, the definitions that apply (already
   pre-filled as filters), a runnable SQL skeleton, and the document topics about these entities.
3. Write ONE SQL statement that joins, filters, aggregates and ranks inside the database: one pre-aggregated subquery per one-to-many branch, LEFT
   JOINed on the grain key (the skeleton shows how). Apply ONLY the filters the question asks for and the definitions provide: never add status or
   scope filters of your own (such as restricting to 'active' records). Never run several small queries and combine their results yourself. A record
   you only know by name is filtered by name in the same statement (the skeleton's WHERE comment shows how): do not run a separate query for its id.
4. Run it with `query_data`, then answer from the result. If it errors, the error says where the column really lives: fix and retry.

## Document questions
Call `library_context` with the question. If `answerable` is false, the documents do not cover it: say so and do not answer from your own knowledge
(first try once more if the question used a company word: `describe` it, or `library_search` with other words). Base the answer on the returned
pages; for the exact quote of a fact use `library_topic_claims`; for a detail a page lacks use `library_source_search` / `library_source`. Never guess
slugs: use only those the tools returned. If topics conflict, say so first. Results are grouped by collection and never mixed. Every collection is
`public` (may be shared with customers) or `internal` (for your own understanding: never quote it or refer to it when talking with customers);
that tag is on every result, obey it.

## How to answer
- Name the entities and columns you used. Say when an item is `pending_confirmation` (not confirmed by the team), and when a definition is only a
  `working` reading.
- Say when data is synthetic, or when a figure is indicative because the model marks its definition as pending confirmation.
- Do not invent entities, columns, joins, example values, rules or facts: only report what the tools returned. Results carry a `next` hint: follow it.
- Never search for restricted data (identity numbers, credentials and the like): state that it is not available.
- Links marked optional or unreliable can be empty: a missing link is not evidence that the related thing does not exist. Say so when your path uses one.
"""


def build_instructions(onto=None):
    """The instructions an agent receives when it connects: the guide, with the model's name."""
    try:
        onto = onto or tools._onto(tools._vid())
    except Exception:
        pass                                                      # no published version yet: the guide alone still works
    name = ((onto or {}).get('ontology') or {}).get('name') or 'Curltale AI'
    return GUIDE.replace('@NAME@', name)


mcp = FastMCP('curltale', instructions=GUIDE, streamable_http_path='/mcp', stateless_http=True, json_response=True,
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))


def refresh_instructions():
    """Re-read the published rules into the instructions (called at startup and after every publish)."""
    mcp._mcp_server.instructions = build_instructions()


TITLES = {'get_access': 'What may I see?', 'search': 'Search the model', 'describe': 'Look something up', 'join_path': 'How entities join',
          'plan_query': 'Plan a data query', 'query_data': 'Run a read-only SQL query'}


def _with_caller(fn, public_name=None):
    """Wrap a tool so it runs as the owner of the bearer token (their entitlements decide what they may see).
    The wrapper takes the MCP request context; the extra parameter is hidden from the tool's public signature."""
    sig = inspect.signature(fn)

    def wrapper(*args, ctx: Context, **kwargs):
        req = ctx.request_context.request
        tok = req.headers.get('authorization', '')[7:] if req is not None else ''
        info = auth.api_token(tok, touch=False)
        actor = info['name'] if info else 'anonymous'
        access.set_caller(actor, info['entitlements'] if info else None)   # unknown -> fail closed
        library_agent.set_principal(library_scope.for_token(info['id'], actor) if info else library_scope.Scope('token', actor), 'agent')
        activity.mcp_session(actor)
        t0 = time.perf_counter()
        shown = kwargs or dict(zip(sig.parameters, args))
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            tools.log_call(public_name or fn.__name__, shown, {'error': f'{type(e).__name__}: {e}'}, (time.perf_counter() - t0) * 1000)
            raise
        tools.log_call(public_name or fn.__name__, shown, result, (time.perf_counter() - t0) * 1000)
        return tools.compact(result)                                     # one compact JSON text: no indentation, no structured duplicate
    wrapper.__name__, wrapper.__doc__, wrapper.__annotations__ = public_name or fn.__name__, fn.__doc__, {**fn.__annotations__, 'ctx': Context, 'return': str}
    wrapper.__signature__ = sig.replace(parameters=[*sig.parameters.values(), inspect.Parameter('ctx', inspect.Parameter.KEYWORD_ONLY, annotation=Context)])
    return wrapper


for tool_name, fn in tools.PUBLIC.items():
    # every tool only reads: clients can auto-approve them instead of asking for each call
    mcp.tool(name=tool_name, title=TITLES.get(tool_name), annotations=ToolAnnotations(title=TITLES.get(tool_name), readOnlyHint=True, destructiveHint=False,
                                                                                    idempotentHint=True, openWorldHint=False), structured_output=False)(_with_caller(fn, tool_name))


def _slim(schema):
    """Input schemas as an agent reads them on every request, without what costs tokens and says nothing: the `title` pydantic adds to each field (it
    repeats the name) and the anyOf-with-null wrapper of an optional parameter (it is optional because it is not required; its default stays)."""
    if isinstance(schema, list):
        return [_slim(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: _slim(v) for k, v in schema.items() if k != 'title'}
    alts = out.get('anyOf')
    if isinstance(alts, list) and len(alts) == 2 and {'type': 'null'} in alts:
        keep = next(a for a in alts if a != {'type': 'null'})
        out.pop('anyOf')
        out = {**keep, **out}
    return out


# tools/list shows the document tools only to a credential that can read at least one collection
from mcp import types as mcp_types
from mcp.server.lowlevel.server import request_ctx
from starlette.concurrency import run_in_threadpool

_list_tools = mcp._mcp_server.request_handlers[mcp_types.ListToolsRequest]


def _may_use_library(tok):
    info = auth.api_token(tok, touch=False)
    return bool(info) and library_agent.available((library_scope.for_token(info['id'], info['name']), 'agent'))


async def _filtered_list_tools(req):
    res = await _list_tools(req)
    try:
        r = request_ctx.get().request
        ok = await run_in_threadpool(_may_use_library, r.headers.get('authorization', '')[7:] if r is not None else '')
    except Exception:
        ok = False
    if not ok:
        res.root.tools = [t for t in res.root.tools if not t.name.startswith('library_')]
    return res


mcp._mcp_server.request_handlers[mcp_types.ListToolsRequest] = _filtered_list_tools

# the document tools: the same wrapper, so they run as the bearer token and are logged like the others
TITLES.update({'library_collections': 'Which document collections can I read?', 'library_context': 'Answer from the documents', 'library_search': 'Find topics', 'library_topic': 'Read a topic',
               'library_topic_claims': 'Claims of a topic, with quotes', 'library_topic_sources': 'Documents behind a topic', 'library_traverse': 'Topics around a topic',
               'library_source_search': 'Search the source wikis', 'library_source': 'Read a source', 'library_vocabulary': 'Topic and relation types'})
for tool_name, fn in library_agent.LIBRARY.items():
    mcp.tool(name=tool_name, title=TITLES[tool_name], annotations=ToolAnnotations(title=TITLES[tool_name], readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False), structured_output=False)(_with_caller(fn, tool_name))
    tools.BY_NAME[tool_name] = fn                                     # Alfred calls tools by name through tools.call

for _t in mcp._tool_manager.list_tools():                          # every tool, model and document, once all are registered
    _t.parameters = _slim(_t.parameters)


@mcp.resource('ontology://guide', title='How to use this server', mime_type='text/markdown')
def guide() -> str:
    """The usage guide plus the live agent rules (the same text as the server instructions)."""
    return build_instructions()


@mcp.prompt(title='Explore a topic of the model')
def explore_model(topic: str) -> str:
    """Explain what the model says about a topic."""
    return (f"Using the Curltale tools, explain what the model says about: {topic}. Start with get_access, then describe / search, "
            f"then join_path where useful. Cite entity and column names, and flag anything pending confirmation.")


@mcp.prompt(title='Answer from the company documents')
def documents_question(question: str) -> str:
    """Answer a question about what the organisation's documents say (policies, procedures, products, prices)."""
    return (f"Answer from the company documents, using the Curltale tools: {question}\n\n"
            "Call library_context with the question. If answerable is false, say the documents do not cover it (after one retry with describe or "
            "library_search if the question used a company word); never answer from your own knowledge. Base the answer on the returned topic pages, "
            "use library_topic_claims for exact quotes and library_source for a missing detail. Cite topics by title, say if sources conflict, and "
            "obey each collection's usage tag: never quote an internal collection to a customer.")


@mcp.prompt(title='Answer a question that needs the documents and the data')
def mixed_question(question: str) -> str:
    """Apply a rule from the documents to the data (eligibility, policies, offers applied to records)."""
    return (f"Answer this question, which needs both a company rule and the data, using the Curltale tools: {question}\n\n"
            "1. get_access. 2. library_context for the rule and its exact conditions (cite the topic). 3. Use the topic's `model` list for the entities "
            "involved, and describe any condition word whose data meaning is unclear. 4. plan_query ONCE with the question restated with the rule's "
            "conditions and all the entities. 5. ONE SQL statement with query_data. Answer with the rule and its source, the figure and the definitions "
            "used, and say if the data is synthetic.")


@mcp.prompt(title='Answer a data question with one query')
def data_question(question: str) -> str:
    """Answer a question about the data with a single SQL statement."""
    return (f"Answer this data question using the Curltale tools: {question}\n\n"
            "Call get_access, map each noun to the most specific entity, call plan_query once, write ONE SQL statement (one pre-aggregated subquery per "
            "one-to-many branch), run it with query_data and answer from the result. Do not run several small queries and merge them yourself. "
            "State which definitions you used and that the data is synthetic.")
