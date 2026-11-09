"""The read-only navigation tools. One registry feeds the MCP server, the REST API and Alfred.

All tools read the PUBLISHED version and enforce the access policy: attributes with agent_access 'none'
(restricted data such as identity numbers) are never returned.
"""
import json
import re
import time
from typing import Annotated, Literal

import sqlglot
from pydantic import Field
from sqlglot import exp

from . import access, activity, config, connectors, filtermatch, datastore, graph, modelview, planner, search as S, settings
from .db import conn
from .ontology import assemble, published_id

PENDING = ('needs_review', 'source_needed')
_cache = {}


def _vid():
    v = published_id()
    if not v:
        raise RuntimeError('no published ontology version')
    return v


def _onto(vid):
    """Assembled model of the published version, cached per version id."""
    if vid not in _cache:
        _cache.clear()
        _cache[vid] = assemble(vid)
    return _cache[vid]


def _find_entity(onto, text):
    """Entity key from an id, name, synonym or table name."""
    text = (text or '').strip()
    if text in onto['entities']:
        return text
    return graph.resolve_entity(_vid(), text) or next((k for k, e in onto['entities'].items() if e.get('semantic_table') == text), None)


def _visible(a):
    return access.can_see(a.get('sensitivity'), a.get('agent_access'))


def _denied_reason(a):
    """Why the caller may not see this attribute. Restricted is never confirmed to exist."""
    if a.get('sensitivity') == 'restricted' or a.get('agent_access') == 'none':
        return None
    return f"requires the '{a['sensitivity']}' entitlement (see get_access)"


def _attr_view(col, a, with_detail=False):
    v = {'column': col, 'business_name': a['business_name'], 'role': a['role'], 'type': a['type'], 'description': a.get('description', ''),
         'sensitivity': a['sensitivity'], 'agent_access': a['agent_access'], 'review_status': a['review_status']}
    for f in ('allowed_values', 'value_labels', 'references', 'format', 'example', 'business_term', 'business_term_description'):
        if a.get(f):
            v[f] = a[f]
    if a['review_status'] in PENDING:
        v['pending_confirmation'] = True
    if with_detail:
        if a.get('tmf_name'):
            v['standard_name'] = a['tmf_name']
        if a.get('custom'):
            v['custom'] = a['custom']
    return v


def _calc_view(code, m, entity=None):
    v = {'code': code, 'name': m['business_name'], 'kind': 'calculated field', 'description': m.get('description', ''), 'type': m.get('type'),
         'sensitivity': m.get('sensitivity'), 'review_status': m.get('review_status')}
    for f in ('unit', 'sql', 'business_term', 'custom'):
        if m.get(f):
            v[f] = m[f]
    if entity:
        v['entity'] = entity
    if m.get('review_status') in PENDING:
        v['pending_confirmation'] = True
    v['note'] = 'not a column: compute it from the data' + ('; the SQL shows how' if m.get('sql') else '')
    return v


def _example_view(k, full=True):
    d = {'id': k['id'], 'title': k.get('title'), 'body': k.get('body') if full else (k.get('body') or '')[:240]}
    for f in ('topic', 'sql', 'params', 'source', 'custom'):
        if k.get(f) and full:
            d[f] = k[f]
    if k.get('applies_to'):
        d['applies_to'] = k['applies_to']
    return d


# ------------------------------------------------------------------ tools
def search_ontology(query: Annotated[str, Field(description='What to look for, in the user\'s own words or as an exact code/name (e.g. "unpaid invoices", "SLA", "invoice_amount")')],
                    types: Annotated[list[str] | None, Field(description='Optional filter. Any of: entity, attribute, calculated, relationship, filter, check, example')] = None,
                    limit: Annotated[int, Field(description='Maximum results, 1-25')] = 10) -> dict:
    """Find things in the ontology (the organisation's dictionary for its data) by meaning and by exact code or name: entities, fields, calculated
    fields, relationships, named filters (what words like 'active' mean), checks and query examples. Use it when a question mentions something and you
    do not know its exact name. Then describe the hit you need, or for a data question go straight to plan_query. Results marked pending_confirmation
    are not yet confirmed by the team: say so when you use them."""
    vid = _vid()
    if isinstance(types, str):                      # models sometimes send '["calculated"]' or 'calculated,entity' instead of a list
        try:
            types = json.loads(types)
        except ValueError:
            types = [t.strip() for t in types.split(',') if t.strip()]
    types = [str(t) for t in types] if isinstance(types, (list, tuple)) and types else None
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 10
    res = S.search(vid, query, types=types, k=min(max(limit, 1), 25))   # filtered by the caller's entitlements
    if not res:
        with conn() as c:
            c.execute("INSERT INTO question_log (source,question,answered) VALUES ('search',%s,false)", (query,))
    return {'query': query, 'count': len(res), 'results': res,
            'note': 'Results marked pending_confirmation are not yet confirmed by the team: say so when you use them.',
            'next': ('describe the result you need for its full definition; for a data question call plan_query with the question and the entities.' if res else
                     'Nothing in the model matches. Try describe with the exact word (it also looks in the company documents)' + _docs_hint(', or library_context if the question is about a policy, product or procedure') + '.')}


def get_entity(entity: Annotated[str, Field(description='Entity id (e.g. customer), name (e.g. "Customer") or a synonym (e.g. "client")')],
               include_attributes: Annotated[bool, Field(description='false returns only the definition and links (about 2k tokens instead of up to 7k)')] = True) -> dict:
    """Full description of one entity: definition, synonyms, what not to confuse it with, table, keys, lifecycle, links, named filters, checks,
    calculated fields, the query examples that apply to it and (optionally) all fields the caller may see."""
    onto = _onto(_vid())
    key = _find_entity(onto, entity)
    if not key:
        return {'error': f'unknown entity "{entity}"', 'hint': 'use search, or describe() with no name to list the entities'}
    e = onto['entities'][key]
    out = {k: e[k] for k in ('id', 'name', 'domain', 'kind', 'definition', 'synonyms', 'not_to_confuse_with', 'semantic_table', 'primary_key',
                             'business_key', 'system_of_record', 'belongs_to', 'lifecycle_states', 'subtype_of', 'subtypes', 'example_questions', 'model_status', 'custom') if e.get(k)}
    if e.get('tmf'):
        out['industry_standard'] = e['tmf']
    out['links_out'] = e.get('relationships_out', [])
    out['links_in'] = e.get('relationships_in', [])
    if e.get('filters'):
        out['filters'] = [{k: f[k] for k in ('name', 'synonyms', 'filter', 'description', 'status', 'source') if f.get(k)} for f in e['filters']]
    if e.get('checks'):
        out['checks'] = [{k: c[k] for k in ('id', 'rule', 'note') if c.get(k)} for c in e['checks']]
    if e.get('calculated'):
        out['calculated_fields'] = [{'code': c, 'name': m['business_name'], 'description': (m.get('description') or '')[:200], **({'sql': m['sql']} if m.get('sql') else {})}
                                    for c, m in e['calculated'].items() if access.can_see(m.get('sensitivity'), m.get('agent_access'))]
    related = modelview.examples_for(onto, [key])
    if related:
        out['examples'] = [_example_view(k, full=False) for k in related]
    shown = {c: a for c, a in e['attributes'].items() if _visible(a) and a['role'] != 'lineage'}
    withheld = sum(1 for c, a in e['attributes'].items() if a['role'] != 'lineage' and not _visible(a) and _denied_reason(a))
    out['attribute_count'] = len(shown)
    if withheld:
        out['withheld_attributes'] = f"{withheld} attribute(s) not shown: your credential lacks an entitlement (see get_access)"
    if include_attributes:
        out['attributes'] = [_attr_view(c, a) for c, a in shown.items()]
    try:                                                          # documents about this entity, if the caller may read any (the model itself knows nothing of them)
        from .library import agent as _lib
        docs = _lib.related_documents(key)
        if docs:
            out['related_documents'] = docs
    except Exception:
        pass
    return out


def get_attribute(entity: Annotated[str, Field(description='Entity id, name or synonym')], column: Annotated[str, Field(description='Column name (e.g. status), calculated field code or business name')]) -> dict:
    """Full detail of one field of an entity (type, allowed values, review status, custom properties), or of one of its calculated fields."""
    onto = _onto(_vid())
    key = _find_entity(onto, entity)
    if not key:
        return {'error': f'unknown entity "{entity}"'}
    e = onto['entities'][key]
    m = e['calculated'].get(column) or next((v for c, v in e['calculated'].items() if v['business_name'].lower() == column.lower()), None)
    if m is not None and access.can_see(m.get('sensitivity'), m.get('agent_access')):
        code = column if column in e['calculated'] else next(c for c, v in e['calculated'].items() if v is m)
        return {'entity': key, 'table': e['semantic_table'], **_calc_view(code, m), 'kind': 'calculated'}
    a = e['attributes'].get(column)
    if a is None:
        match = [c for c, x in e['attributes'].items() if x['business_name'].lower() == column.lower()]
        a, column = (e['attributes'][match[0]], match[0]) if match else (None, column)
    if a is not None and not _visible(a) and _denied_reason(a):
        return {'error': 'not entitled', 'reason': _denied_reason(a)}
    if a is None or not _visible(a):
        return {'error': f'field "{column}" not found on {key}'}
    return {'entity': key, 'table': e['semantic_table'], **_attr_view(column, a, with_detail=True)}


def list_entities(domain: Annotated[str | None, Field(description='Optional domain to list only (domain names are returned by the overview)')] = None) -> dict:
    """Overview of the model: all domains and their entities (name, kind, one-line definition). Start here to orient yourself."""
    onto = _onto(_vid())
    doms = {}
    for d, keys in modelview.domains(onto).items():
        if domain and d.lower() != domain.lower():
            continue
        doms[d] = {'entities': [{'id': k, 'name': onto['entities'][k]['name'], 'kind': onto['entities'][k]['kind'], 'table': onto['entities'][k]['semantic_table'],
                                 'definition': (onto['entities'][k].get('definition') or '')[:160]} for k in keys]}
    return {'version': onto['ontology'].get('version'), 'domains': doms}


def _term(onto, text):
    """A word the model knows: an entity's name or synonym."""
    t = text.strip().lower()
    ents = [{'id': k, 'name': e['name']} for k, e in onto['entities'].items() if t == k or t == e['name'].lower() or t in [s.lower() for s in e.get('synonyms', [])]]
    return {'term': text, 'entities': ents}


def find_join_path(from_entity: Annotated[str, Field(description='Entity id, name or synonym to start from')], to_entity: Annotated[str, Field(description='Entity id, name or synonym to reach')]) -> dict:
    """How to join two entities: the best path of relationships with the exact join conditions, plus an all-confirmed alternative
    when the best path uses optional or unreliable links."""
    res = graph.find_path(_vid(), from_entity, to_entity)
    onto = _onto(_vid())
    ends = [k for k in (res.get('from'), res.get('to')) if k in onto['entities']]
    if len(ends) == 2:                                              # documents about either end often say how the two really connect
        try:
            from .library import agent as _lib
            docs = _lib.related_documents(ends[0]) + _lib.related_documents(ends[1])
            if docs:
                res['related_documents'] = docs
        except Exception:
            pass
    return res


def get_neighbors(entity: Annotated[str, Field(description='Entity id, name or synonym')]) -> dict:
    """All entities directly linked to this one, with join, cardinality and reliability of each link."""
    vid = _vid()
    key = graph.resolve_entity(vid, entity)
    if not key:
        return {'error': f'unknown entity "{entity}"'}
    return {'entity': key, 'links': [{'rid': n['rid'], 'direction': n['direction'], 'predicate': n['predicate'], 'other': n['other_name'],
                                      'cardinality': n['cardinality'], 'join': n['join'], 'status': n['status'], 'note': n['note']}
                                     for n in graph.neighbors(vid, key)]}


def get_calculated(code: Annotated[str | None, Field(description='Calculated field code or name; omit to list all')] = None,
                   entity: Annotated[str | None, Field(description='List only the calculated fields of this entity')] = None) -> dict:
    """Calculated fields: values that are not columns but computed from the data, with their meaning and (when the model gives it) the SQL that
    computes them. Treat one whose review status is pending as indicative."""
    onto = _onto(_vid())
    cat = modelview.calculated(onto)
    if code:
        k = cat.get(code) or next((v for c, v in cat.items() if v['name'].lower() == code.lower()), None)
        if not k:
            return {'error': f'unknown calculated field "{code}"', 'known': list(cat)[:60]}
        if not access.can_see(k.get('sensitivity'), k.get('agent_access')):
            return {'error': 'not entitled', 'reason': f"requires the '{k['sensitivity']}' entitlement (see get_access)"}
        return _calc_view(next(c for c, v in cat.items() if v is k), k, k['entity'])
    items = [{'code': c, 'name': k['name'], 'entity': k['entity'], **({'unit': k['unit']} if k.get('unit') else {})}
             for c, k in cat.items() if (not entity or k['entity'].lower().startswith(entity.lower())) and access.can_see(k.get('sensitivity'), k.get('agent_access'))]
    return {'count': len(items), 'calculated_fields': items}


def _filters(onto, text=None):
    """Named filters (what a word means as a condition on an entity's table) matching a name, a synonym or an entity; all when text is empty."""
    t = (text or '').strip().lower()
    key = _find_entity(onto, text) if t else None
    out = []
    for k, f in modelview.filters(onto):
        if not t or t == f['name'].lower() or t in [x.lower() for x in f.get('synonyms', [])] or k == key:
            out.append({'entity': onto['entities'][k]['name'], **{x: f[x] for x in ('name', 'synonyms', 'filter', 'description', 'status', 'source') if f.get(x)}})
    return out


def _examples(onto, name=None):
    """Query examples: by id, by title, or by words; all of them (short) when no name."""
    ex = onto['examples']
    if not name:
        return {'count': len(ex), 'examples': [_example_view(k, full=False) | {'body': (k.get('body') or '')[:120]} for k in ex]}
    n = name.strip().lower()
    hit = [k for k in ex if k['id'].lower() == n or (k.get('title') or '').lower() == n]
    if not hit:
        words = [w for w in re.split(r'\W+', n) if len(w) > 2]
        hit = [k for k in ex if any(w in ((k.get('title') or '') + ' ' + (k.get('topic') or '') + ' ' + (k.get('body') or '')).lower() for w in words)][:8]
    return {'examples': [_example_view(k) for k in hit]} if hit else {'error': f'no example matches "{name}"'}


KINDS = ('auto', 'entity', 'field', 'calculated', 'filter', 'example')


def _docs_hint(text):
    """A next-step hint that mentions the document tools only to a caller who has them."""
    try:
        from .library import agent as _lib
        return text if _lib.available() else ''
    except Exception:
        return ''


def describe(name: Annotated[str | None, Field(description="What to look up: an entity (id, name or synonym), a field as 'entity.column', a calculated field code, a named filter (what 'open', 'active'... mean as a condition), a query example (by id or words), or a word the model or the documents know. Omit to list.")] = None,
             kind: Annotated[Literal['auto', 'entity', 'field', 'calculated', 'filter', 'example'], Field(description="'auto' detects what the name is. With no name, kind lists: entity = overview of domains and entities, calculated, filter or example.")] = 'auto',
             include_attributes: Annotated[bool, Field(description='For an entity: false returns only the definition and links (about 2k tokens instead of up to 7k)')] = True) -> dict:
    """Understand what a word means here, or look up anything the ontology defines, by name. Use it when a question turns on a business word (active,
    unpaid, premium, a product or plan name, an abbreviation) whose exact meaning you are not sure of: never assume one. Returns an entity (definition,
    synonyms, what not to confuse it with, table, keys, links, named filters, checks, calculated fields, applicable query examples, fields with meaning
    and allowed values), a field ('entity.column'), a calculated field, a named filter (the exact condition a word stands for), a query example (with
    SQL) or a term; a name the model does not know is looked up in the company documents you may read. The kind is detected; pass kind to force it.
    With no name it lists (kind='entity': the overview of the whole model). Next: plan_query for data about it; library_context for what the documents say."""
    onto = _onto(_vid())
    k = (kind or 'auto').lower()
    k = 'calculated' if k in ('measure', 'kpi', 'metric') else k              # the words agents often use for a calculated value
    if k not in KINDS:
        return {'error': f"kind must be one of {', '.join(KINDS)}"}
    if not name or not str(name).strip():
        if k in ('auto', 'entity'):
            return {'resolved_as': 'overview', **list_entities()}
        if k == 'calculated':
            return {'resolved_as': 'calculated', **get_calculated()}
        if k == 'filter':
            return {'resolved_as': 'filter', 'filters': _filters(onto)}
        if k == 'example':
            return {'resolved_as': 'example', **_examples(onto)}
        return {'error': 'give a name, or use kind entity / calculated / filter / example to list'}
    name = str(name).strip()
    if k in ('auto', 'example'):
        entry = next((x for x in onto['examples'] if x['id'].lower() == name.lower()), None)
        if entry:
            return {'resolved_as': 'example', 'examples': [_example_view(entry)]}
        if k == 'example':
            return {'resolved_as': 'example', **_examples(onto, name)}
    if k in ('auto', 'field', 'calculated') and '.' in name:
        ent, col = name.rsplit('.', 1)
        key = _find_entity(onto, ent)
        if key:
            r = get_attribute(key, col)
            return {'resolved_as': r.get('kind', 'field') if 'error' not in r else 'field', **r}
    if k in ('auto', 'entity'):
        key = _find_entity(onto, name)
        if key:
            return {'resolved_as': 'entity', **get_entity(key, include_attributes),
                    'next': 'For data about it call plan_query with the question and this entity' + _docs_hint('; for what the company documents say about it, library_context') + '.'}
        if k == 'entity':
            return {'error': f'unknown entity "{name}"', 'suggestions': search_ontology(name, ['entity'], 5)['results']}
    if k in ('auto', 'calculated'):
        r = get_calculated(name)
        if 'error' not in r:
            return {'resolved_as': 'calculated', **r}
        if k == 'calculated':
            return r
    if k in ('auto', 'filter'):
        f = _filters(onto, name)
        if f:
            return {'resolved_as': 'filter', 'filters': f, 'next': 'This is the exact condition the word stands for. plan_query applies it for you when the question uses the word.'}
    if k == 'auto':
        t = _term(onto, name)
        if t['entities']:
            return {'resolved_as': 'term', **t, 'next': 'The word stands for the entities listed: use them in plan_query for a data question' + _docs_hint(', or ask library_context what the documents say') + '.'}
        ex = _examples(onto, name)
        if 'examples' in ex:
            return {'resolved_as': 'example', **ex}
        try:                                                      # an abbreviation or a name from the documents: which topics, and which entities they are about
            from .library import agent as _lib
            docs = _lib.lookup_name(name)
            if docs:
                return {'resolved_as': 'document', 'name': name, 'found_in_documents': docs,
                        'note': "Not a name in the model, but the documents you may read mention it. A topic's `model` list says which entities it is about: use those.",
                        'next': "Read what the documents say with library_context (or library_topic for one of these topics); for data, use the entities in a topic's `model` list with plan_query."}
        except Exception:
            pass
    found = search_ontology(name, None, 5)
    return {'error': f'nothing in the model is called "{name}"', 'did_you_mean': [{'id': x['id'], 'type': x['type'], 'title': x['title']} for x in found['results']],
            'next': 'Pick one of did_you_mean if it is what was meant' + _docs_hint(', or ask library_context: it may be a product, plan or policy described only in the documents') + '; otherwise say the term is not known.'}


def join_path(from_entity: Annotated[str, Field(description='Entity id, name or synonym to start from')],
              to_entity: Annotated[str | None, Field(description='Entity id, name or synonym to reach. Omit to list everything directly linked to from_entity')] = None) -> dict:
    """How two entities of the ontology connect. With a target: the best path with the exact join conditions, how far each link can be trusted, an
    all-confirmed alternative when the best path uses optional or unreliable links, warnings about row multiplication, and the documents about either
    end that you may read. Without a target: every entity directly linked to the first, with join, cardinality and reliability. For a data question you
    do not need it: plan_query already gives the joins. A link marked optional or unreliable can be empty: that is not evidence that nothing is related."""
    if to_entity and str(to_entity).strip():
        return find_join_path(from_entity, to_entity)
    return {'resolved_as': 'neighbors', **get_neighbors(from_entity)}


# ------------------------------------------------------------------ data tools (synthetic database)


def _plain(x):
    """Dates, decimals and the like become plain JSON values (models and MCP clients only speak JSON)."""
    return json.loads(json.dumps(x, default=str))


def _data_gate():
    """None when the caller may query data; otherwise the error to return."""
    name, ent = access.caller()
    if not ent['data']:
        return {'error': 'not entitled', 'reason': "your credential may not query data (it is structure-only). An admin can grant the 'data' entitlement."}
    row = datastore.active_connector()
    if row and not {**connectors.DEFAULT_OPTIONS, **(row['options'] or {})}['agents_may_read_rows']:
        return {'error': 'not allowed', 'reason': 'this connector does not let agents read rows. Use plan_query to see how the question maps onto the model, and ask a person with console access to run it.'}
    if not datastore.available():
        return {'error': 'no database', 'reason': ('the database behind the connector cannot be reached right now' if row else 'no synthetic database has been built yet (an admin can build it on the Data → Database page)')}
    return None


def plan_query(question: Annotated[str | None, Field(description="The user's question, verbatim: used to pick columns, the definitions that apply (open, active, unpaid...) and related calculated fields")] = None,
               entities: Annotated[list[str] | None, Field(description='The entities involved, as ids, names or synonyms. Use the MOST SPECIFIC entity: if one entity unifies several kinds (a supertype) and another is one of those kinds, use the specific one')] = None,
               grain: Annotated[str | None, Field(description='What one result row represents (an entity). Omit to let the planner choose the entity the others hang off')] = None) -> dict:
    """START HERE for any data question that involves one or more entities (counts, totals, comparisons, rankings, filters across entities). One call
    returns everything needed to write ONE SQL statement: for each entity the table,
    columns (with meaning and exact allowed values), how it joins to the grain (join key or path, fan-out warnings), the definitions
    that apply (what words like 'open', 'active' or 'unpaid' mean as a filter, already pre-filled), the model's own query patterns, and a ready SQL
    skeleton with one pre-aggregated subquery per one-to-many branch, plus the document topics about these entities. Pass the user's question and ALL
    the entities involved, using the MOST SPECIFIC entity for each noun; optionally the grain (what one result row represents). It resolves the
    question's words itself: no need to describe them first. If the question applies a company rule (eligibility, a policy, an offer's conditions), get
    the rule with library_context first and restate the question with its conditions. Then complete the skeleton and run it with query_data as ONE
    statement: do NOT run several small queries and combine them yourself, and do not add filters the question and the definitions do not ask for."""
    vid = _vid()
    onto = _onto(vid)
    if isinstance(entities, str):
        try:
            entities = json.loads(entities)
        except ValueError:
            entities = [e.strip() for e in entities.split(',') if e.strip()]
    res = planner.plan(onto, vid, lambda n: _find_entity(onto, n), entities, grain, question, access.caller()[1])
    try:
        drv = datastore.driver()
        res['sql_dialect'] = drv.label
        if drv.dialect != 'postgres' and isinstance(res.get('sql_skeleton'), str):      # the model keeps its SQL in PostgreSQL syntax; the agent gets it in the database's own
            res['sql_skeleton'] = to_dialect(drv, res['sql_skeleton'])
            res['sql_note'] = f'SQL is written for {drv.label}. Keep to its syntax in the statement you run.'
    except Exception:
        pass
    res['next'] = ('Complete the skeleton into ONE statement and run it with query_data. Apply only the pre-filled definitions and the filters the question asks for.'
                   + (' Some document topics concern these entities: if the question depends on a rule or policy, read them with library_context or library_topic first.' if res.get('documents') else ''))
    return _plain(res)


def to_dialect(drv, sql):
    """A SQL text with '--' hint lines, translated: the hint lines move to the end (they are advice, not syntax)."""
    lines = sql.split('\n')
    hints = [l for l in lines if l.strip().startswith('--')]
    out = drv.to_dialect('\n'.join(l for l in lines if not l.strip().startswith('--')))
    return out + ('\n' + '\n'.join(hints) if hints else '')


def _sql_hint(message):
    """A missing column or table: say where it does exist (one retry instead of a round of lookups)."""
    import re
    onto = _onto(_vid())
    ent = access.caller()[1]
    m = re.search(r'column "?(?:\w+\.)?(\w+)"? does not exist', message)
    if m:
        col = m.group(1).lower()
        where = [f"{e['semantic_table']}.{c}" for e in onto['entities'].values() for c, a in e['attributes'].items()
                 if c.lower() == col and access.can_see(a.get('sensitivity'), a.get('agent_access'), ent)]
        near = [f"{e['semantic_table']}.{c}" for e in onto['entities'].values() for c, a in e['attributes'].items()
                if col in c.lower() and c.lower() != col and access.can_see(a.get('sensitivity'), a.get('agent_access'), ent)][:6]
        if where or near:
            return {'hint': (f"column {col} exists in: {', '.join(where)}. " if where else '') + (f"similar columns: {', '.join(near)}" if near else '')}
    m = re.search(r'relation "?([\w.]+)"? does not exist', message)
    if m:
        t = m.group(1).split('.')[-1].lower()
        near = [e['semantic_table'] for e in onto['entities'].values() if t in e['semantic_table'].lower() or e['semantic_table'].split('.')[-1] in t][:6]
        return {'hint': f"tables: {', '.join(near)}" if near else 'use describe to find the table of an entity'}
    return {}


def query_data(sql: Annotated[str, Field(description='ONE read-only statement (SELECT / WITH) with schema-qualified tables. List columns explicitly.')]) -> dict:
    """Run ONE read-only SQL statement on the database that implements the ontology; one statement answers one question. Call plan_query first for anything beyond a trivial lookup: it gives
    the tables, columns, join keys and definitions, so a single statement with joins, CTEs, GROUP BY, window functions and ORDER BY/LIMIT can do all the
    work in the database. Tables are schema-qualified (describe gives each entity's table). Write the SQL in the dialect plan_query reports (sql_dialect). Returns a limited number of rows: when there are more, the result says how many in all (total_rows). You see only the columns your
    credential is entitled to: list columns explicitly instead of SELECT *. Figures the model marks as not yet defined are indicative.
    When a filter on a non-key text column selects records whose key the query then uses (for example a lookup by a descriptive value), the result
    includes filter_matches: how many distinct records that filter matched, with a few examples."""
    gate = _data_gate()
    if gate:
        return gate
    try:
        res = datastore.console(sql, access.caller()[1], max_rows=settings.get('AGENT_MAX_ROWS'))
    except datastore.DataError as e:
        return {'error': str(e), **_sql_hint(str(e))}
    rows = res['rows']
    matches = _filter_matches(sql)
    return _plain({'columns': res['columns'], 'row_count': len(rows), **(_cut(sql, len(rows)) if res['truncated'] else {}),
                   **({'filter_matches': matches} if matches else {}),
                   **({} if datastore.active_connector() else {'note': 'Synthetic demo data generated from the model.'}),
                   **({'masked_columns': res['masked_columns'], 'masked_note': 'These columns are masked: values are stable hashes (equal values give equal hashes), not the real values.'} if res.get('masked_columns') else {}),
                   'rows': rows})                                  # last: if a long result is cut on its way to a model, the notes above survive


def _cut(sql, shown):
    """A result with more rows than an agent receives: how many there are in all (counted with the caller's own rules), and what to do about it."""
    total, ordered = None, True
    try:
        dialect = datastore.driver().dialect
        q = sqlglot.parse_one(sql.strip().rstrip(';'), read=dialect)
        ordered = q.args.get('order') is not None
        cte = q.args.get('with')
        if cte is not None:                                        # CTEs move to the outer statement (not every database allows them in a subquery)
            q.set('with', None)
        if q.args.get('limit') is None:
            q.set('order', None)
        outer = exp.select(exp.Count(this=exp.Star()).as_('n')).from_(q.subquery('q'))
        if cte is not None:
            outer.set('with', cte)
        total = int(datastore.console(outer.sql(dialect=dialect), access.caller()[1])['rows'][0]['n'])
    except Exception:
        pass                                                       # the count is information only: without it, say "more than"
    if total is not None and total <= shown:
        total = None
    many = f'all {total}' if total else f'more than {shown}'
    return {'truncated': True, **({'total_rows': total} if total else {}),
            'truncated_note': f'This query has {"%d rows" % total if total else "more than %d rows" % shown}; only the first {shown} are returned'
                              f'{"" if ordered else ", in no particular order"}. Results are not paged: to cover {many}, count or aggregate in SQL '
                              f'(COUNT, SUM, GROUP BY) or narrow the query with filters.'}


def _filter_matches(sql):
    """Lookup filters that selected several distinct records (information only; any failure here leaves the result as it is)."""
    try:
        ent, cat = access.caller()[1], datastore.current_catalog()
        if cat is None:
            return []
        return filtermatch.report(sql.strip().rstrip(';'), cat, ent, datastore.driver().dialect, lambda q: datastore.console(q, ent, cat)['rows'])
    except Exception:
        return []


def get_access() -> dict:
    """Call this ONCE, first. Who you are to this server and what you may see: which classes of information your credential is entitled to
    (confidential, personal, restricted), whether you may query data, and the SQL dialect. Whatever is not granted is absent from every answer: say it
    is withheld, never treat it as empty or zero. The server holds two things: the ontology (what the organisation's data and words mean, and the data
    tools) and, if you hold collections, the company documents (start those with library_context)."""
    name, ent = access.caller()
    policy = _onto(_vid()).get('access_policy', {})
    meaning = {lvl: (policy.get(lvl) or '') + ('' if ent.get(lvl, True) else ' [NOT GRANTED: hidden from every answer]') for lvl in ('internal', 'confidential', 'personal', 'restricted')}
    meaning['data'] = ('may query the database with plan_query / query_data; rows are filtered by the same entitlements' if ent['data'] else 'structure only: you cannot query data')
    try:
        meaning['sql_dialect'] = f'the database speaks {datastore.driver().label}: write SQL in that dialect (plan_query returns its skeleton already in it)'
    except Exception:
        pass
    return {'caller': name, 'entitlements': {'internal': True, **ent}, 'meaning': meaning,
            'note': 'Entitlements decide which definitions and which data columns you can see. A withheld item is not empty or zero: say it is withheld.'}


# the public surface: six generic tools. Everything else above is internal (and importable for tests).
PUBLIC = {'get_access': get_access, 'search': search_ontology, 'describe': describe, 'join_path': join_path, 'plan_query': plan_query, 'query_data': query_data}
TOOLS = list(PUBLIC.values())
BY_NAME = dict(PUBLIC)


def compact(result):
    """The text an agent receives for a tool result: JSON without indentation or spaces after separators (whitespace costs tokens on every call)."""
    return json.dumps(result, ensure_ascii=False, separators=(',', ':'), default=str)


def _summ(args):
    t = json.dumps(args or {}, ensure_ascii=False, default=str)
    return args if len(t) <= 600 else {'_truncated': t[:600]}


def log_call(name, args, result, ms):
    """One activity event for a tool call (shared by Alfred's calls and MCP's)."""
    ok = not (isinstance(result, dict) and result.get('error'))
    extra = {}
    if isinstance(result, dict):
        if not ok:
            extra['error'] = str(result['error'])[:300]
        if 'row_count' in result:
            extra['rows'] = result['row_count']
        if isinstance(result.get('results'), list):
            extra['hits'] = len(result['results'])
    text = json.dumps(result, default=str, ensure_ascii=False) if result is not None else ''
    limit = settings.get('ACTIVITY_RESULT_CHARS')
    if limit and result is not None:                       # what the caller received, cut to a size the log can hold (0: results are not kept)
        extra['result'] = json.loads(text) if len(text) <= limit else text[:limit]
        extra['truncated'] = len(text) > limit
    activity.log('tool', name, ms=ms, ok=ok, args=_summ(args), chars=len(text), **extra)


def call(name, args):
    t0 = time.perf_counter()
    result = _call(name, args)
    log_call(name, args, result, (time.perf_counter() - t0) * 1000)
    return result


def _call(name, args):
    fn = BY_NAME.get(name)
    if not fn:
        return {'error': f'unknown tool {name}'}
    try:
        return fn(**(args or {}))
    except TypeError as e:
        return {'error': f'bad arguments: {e}'}
    except RuntimeError as e:
        return {'error': str(e)}
    except Exception as e:                           # a tool must never take the whole conversation down: tell the model what went wrong
        return {'error': f'{type(e).__name__}: {str(e).splitlines()[0][:200]}'}


def invalidate():
    _cache.clear()
