"""What agents (MCP clients, Alfred) may do with the Library: agent retrieval over the knowledge base, collection by collection. All read only.

    library_context        START HERE. Search -> rerank -> full topic pages with the matched passage window -> graph of related topics (+ answerable)
    library_search         lightweight topic discovery: slugs, titles, summaries, the best matching chunk
    library_topic          one full topic page: Markdown, aliases, typed links both ways, its sources, the model entities it is about
    library_topic_claims   the atomic claims of a topic, each with its exact quote, source and source wiki version
    library_topic_sources  the source documents behind a topic, with the wiki version used
    library_traverse       BFS over typed topic links from one or more topics
    library_source_search  search the approved source wikis (the verified page written from each document)
    library_source         read a source's approved wiki (whole, around a passage, or searched inside), or its original text
    library_vocabulary     the topic types and relation types of a collection
    library_collections    which collections can I read?

Chunks are how the system FINDS, topic pages are what it RETURNS: topic pages, source wikis, original text, claims and typed
links are all chunked or embedded and searched by meaning and by words.

Rules that hold for every answer:
  * An agent sees only collections its principal holds a grant on AND that allow agents to read them (Alfred also needs the collection to allow AI).
  * Results are grouped by collection and never merged or ranked across collections.
  * Every collection block carries its usage tag and the one-line note that goes with it (public: may be shared with customers; internal: for your own
    understanding, not to be quoted to customers).
  * A collection that does not exist and one the agent may not read answer exactly the same.
"""
import functools
import re
from contextvars import ContextVar
from typing import Annotated, Literal
from pydantic import Field
from .. import config, embeddings, settings, vectors
from ..db import conn
from . import bridge, index, rerank, retrieval, scope as S, topics, vocab

CURRENT: ContextVar = ContextVar('library_principal', default=None)       # (Scope, 'agent' | 'alfred')
NOTE = ('Every result names its collection and says whether it is `public` (may be shared with customers) or `internal` (for your own understanding: do not quote it '
        'or refer to it when you talk with customers).')


def _safe(fn):
    """An expected problem (no such collection, nothing readable) is an answer the agent can read, not a crashed call."""
    @functools.wraps(fn)
    def run(*a, **k):
        try:
            return fn(*a, **k)
        except RuntimeError as e:
            return {'error': str(e)}
    return run


def set_principal(scope, who='agent'):
    return CURRENT.set((scope, who))


def _principal():
    p = CURRENT.get()
    if not p or not settings.get('LIBRARY_ENABLED'):
        raise RuntimeError('the document library is not available to you')
    return p


def readable(principal=None):
    """The collections this principal's agent may read, as handles."""
    sc, who = principal or _principal()
    out = []
    with conn() as c:
        rows = c.execute('SELECT id FROM library.collection WHERE id::text = ANY(%s) ORDER BY lower(name)', (list(sc.grants),)).fetchall()
    for r in rows:
        try:
            col = S.open_collection(sc, r['id'])
        except Exception:
            continue
        if col.agents_may_read and (who != 'alfred' or col.allow_ai):
            out.append(col)
    return out


def available(principal=None):
    """Is there anything for this principal? (The tools are only offered when there is.)"""
    try:
        return bool(readable(principal))
    except RuntimeError:
        return False


def _pick(ref):
    cols = readable()
    ref = (ref or '').strip().lower()
    for col in cols:
        if ref in (col.id.lower(), col.slug.lower(), col.name.lower()):
            return col
    raise RuntimeError(f"no such collection: {ref or '(none given)'}. library_collections lists the ones you can read.")


def _head(col):
    return {'collection': col.name, 'collection_id': col.id, 'usage': col.usage, 'usage_note': S.USAGE_NOTE[col.usage]}


def _model_of(col, tid):
    """The model entities and fields a topic is about, as this caller may see them (a withheld field is left out)."""
    try:
        return [{'kind': m['kind'], 'ref': m['ref'], 'name': m['name'], 'relation': m['label']} for m in bridge.resolve(bridge.refs_of(tid, col.id)) if m['status'] == 'confirmed' and m['in_model'] is not False]
    except Exception:
        return []


def _link(col, slug):
    return f'/library/topics/{slug}?c={col.id}'


def _source_link(col, sid):
    return f'/library/sources/{sid}?c={col.id}'


# ---------------------------------------------------------------- tools
def _tokens(text):
    return max(1, len(text or '') // 4)


def _topic_hits(col, query, k):
    """The topics of a collection that answer a question, best first: the topic page chunks found by meaning and words (the matching unit), plus topics
    whose identity (title, aliases, summary, claims) matches, then an optional reranker. -> [{topic_id, slug, title, topic_type, summary, score, idx, text}]"""
    best = {}
    for h in index.search_topic_chunks(col.id, query, pool=k * 10):
        if h['topic_id'] not in best:
            best[h['topic_id']] = {'topic_id': h['topic_id'], 'slug': h['slug'], 'title': h['title'], 'topic_type': h['topic_type'], 'summary': h['summary'], 'score': h['score'], 'idx': h['idx'], 'text': h['text'],
                                   'section': ' > '.join(h.get('heading_path') or []) or None}
        else:
            best[h['topic_id']]['score'] += h['score'] * 0.1                                  # more matching chunks: a little more relevant
    for rank, t in enumerate(topics.search(col.id, query, k)):                                 # identity search: catches a topic named like the question
        row = topics.find(col.id, t['slug'])
        if not row:
            continue
        if row['id'] in best:
            best[row['id']]['score'] += 1 / (60 + rank)
        else:
            best[row['id']] = {'topic_id': row['id'], 'slug': row['slug'], 'title': row['title'], 'topic_type': row['topic_type'], 'summary': row['summary'], 'score': 1 / (60 + rank), 'idx': None, 'text': row['summary'], 'section': None}
    ranked = sorted(best.values(), key=lambda x: -x['score'])[:max(k * 2, k)]
    if ranked and rerank.available():
        order = rerank.rerank(query, [f"{r['title']}. {r['text']}" for r in ranked])
        if order:
            ranked = [ranked[i] for i in order]
    return ranked[:k]


SOURCE_REF = re.compile(r'`?\[source:\s*([0-9a-fA-F-]{36})\]`?')


def _short_citations(col, markdown, legend):
    """`[source: <uuid>]` (about 15 tokens each, on every line of a page) becomes [s1], [s2]...; legend maps each short reference to the source once."""
    def one(m):
        sid = m.group(1).lower()
        if sid not in legend['by_id']:
            ref = f"s{len(legend['by_id']) + 1}"
            legend['by_id'][sid] = ref
            legend['list'].append({'ref': ref, 'source_id': sid})
        return f"[{legend['by_id'][sid]}]"
    return SOURCE_REF.sub(one, markdown or '')


def _legend(col, legend):
    """The sources a set of pages cites: ref, source_id, name, link (names looked up once)."""
    if not legend['list']:
        return []
    with conn() as c:
        names = {str(r['id']): r['name'] for r in c.execute('SELECT id, coalesce(title, name) AS name FROM library.source WHERE collection_id=%s AND id::text = ANY(%s)',
                                                             (col.id, [x['source_id'] for x in legend['list']])).fetchall()}
    return [{**x, 'name': names.get(x['source_id'], '(removed source)'), 'link': _source_link(col, x['source_id'])} for x in legend['list']]


def _graph(col, seeds, depth=1, max_topics=20, relations=None):
    """Multi-seed BFS over typed links, inside the collection. -> {'nodes', 'edges', 'truncated'}"""
    rel = [r.strip() for r in (relations or []) if r and r.strip()]
    with conn() as c:
        rows = c.execute("SELECT id, slug, title, topic_type, summary FROM library.topic WHERE collection_id=%s AND status='active' AND id = ANY(%s)", (col.id, list(seeds))).fetchall()
        nodes = {r['id']: {'topic': r['slug'], 'title': r['title'], 'type': r['topic_type'], 'summary': r['summary'], 'link': _link(col, r['slug']), 'is_seed': True} for r in rows}
        edges, seen, frontier, truncated = [], set(), set(nodes), False
        for _ in range(max(1, min(int(depth), 3))):
            if not frontier or len(nodes) >= max_topics:
                break
            links = c.execute("SELECT l.from_topic, l.to_topic, l.relation, l.context FROM library.topic_link l JOIN library.topic a ON a.id=l.from_topic AND a.status='active' "
                              "JOIN library.topic b ON b.id=l.to_topic AND b.status='active' WHERE l.collection_id=%s AND (l.from_topic = ANY(%s) OR l.to_topic = ANY(%s)) "
                              "AND (cardinality(%s::text[]) = 0 OR l.relation = ANY(%s))", (col.id, list(frontier), list(frontier), rel, rel)).fetchall()
            nxt = set()
            for l in links:
                for x in (l['from_topic'], l['to_topic']):
                    if x not in nodes:
                        if len(nodes) >= max_topics:
                            truncated = True
                            continue
                        r = c.execute('SELECT slug, title, topic_type, summary FROM library.topic WHERE id=%s', (x,)).fetchone()
                        nodes[x] = {'topic': r['slug'], 'title': r['title'], 'type': r['topic_type'], 'summary': r['summary'], 'link': _link(col, r['slug']), 'is_seed': False}
                        nxt.add(x)
                key = (l['from_topic'], l['to_topic'], l['relation'])
                if key not in seen and l['from_topic'] in nodes and l['to_topic'] in nodes:
                    seen.add(key)
                    edges.append({'from': nodes[l['from_topic']]['topic'], 'relation': l['relation'], 'to': nodes[l['to_topic']]['topic'], 'context': l['context']})
            frontier = nxt
    return {'nodes': list(nodes.values()), 'edges': edges, 'truncated': truncated}


# ---------------------------------------------------------------- tools
@_safe
def library_collections() -> dict:
    """The document collections you may read, each with its usage tag, what it holds and how many topics it has. Call it when you need the list.
    Each result carries its collection's usage tag: `public` may be shared with customers; `internal` is for your understanding only, do not quote it to customers."""
    cols = readable()
    out = []
    with conn() as c:
        for col in cols:
            n = c.execute("SELECT (SELECT count(*) FROM library.topic WHERE collection_id=%s AND status='active') AS topics, (SELECT count(*) FROM library.source WHERE collection_id=%s AND status='ready') AS sources", (col.id, col.id)).fetchone()
            out.append({**_head(col), 'slug': col.slug, 'description': col.description, 'topics': n['topics'], 'documents': n['sources']})
    return {'collections': out, 'note': NOTE if out else 'You have no readable collections.'}


@_safe
def library_context(query: Annotated[str, Field(description="The user's question, as asked")],
                    collection: Annotated[str | None, Field(description='Limit to one collection (name, slug or id); omit to search all you may read')] = None,
                    top_n: Annotated[int, Field(description='How many topic pages per collection (full pages: each adds a page of text)', ge=1, le=10)] = 5,
                    depth: Annotated[int, Field(description='Hops of graph traversal from the found topics', ge=1, le=3)] = 1) -> dict:
    """START HERE for any question the company documents may answer: what the organisation says about a policy, procedure, product, plan, price,
    offer, eligibility rule or how-to. Pass the question as asked. Searches the topic pages by meaning and words, reranks, and returns for each collection:
    `topics` (the FULL Markdown of each matching topic page, `matched_section` naming where the question matched; pages cite sources as [s1], [s2]...,
    listed once in `sources`), `related` (more topics linked to these, with the typed links), `relationships` (typed links whose meaning matches the
    question), and `answerable`. If `answerable` is false, the knowledge base does not cover the question: say so, do not answer from your own knowledge.
    Cite topics by title; the claims behind a page are in library_topic_claims, the documents in library_topic_sources. A topic's `model` list names the
    data entities it concerns: to apply a rule to the data, pass them to plan_query with the rule's conditions.
    Each result carries its collection's usage tag: `public` may be shared with customers; `internal` is for your understanding only, do not quote it to customers."""
    cols = [_pick(collection)] if collection else readable()
    k = max(1, min(int(top_n), 10))
    blocks, total = [], 0
    for col in cols:
        hits = _topic_hits(col, query, k)
        out, legend = [], {'by_id': {}, 'list': []}
        for h in hits:
            ver, md = index.published(col.id, h['topic_id'])
            if not md:
                continue
            md = _short_citations(col, md, legend)
            total += _tokens(md)
            out.append({'topic': h['slug'], 'title': h['title'], 'type': h['topic_type'], 'kind_of': bridge.kinds_of(col.id, [h['topic_id']]).get(h['topic_id'], []), 'summary': h['summary'], 'score': round(h['score'], 4), 'link': _link(col, h['slug']),
                        'model': _model_of(col, h['topic_id']), **({'matched_section': h['section']} if h.get('section') else {}), 'markdown': md})
        if not out:
            continue
        ids = [topics.find(col.id, t['topic'])['id'] for t in out]
        graph = _graph(col, ids, depth, max_topics=len(ids) + 10)
        graph['nodes'] = [n for n in graph['nodes'] if not n['is_seed']]                    # the found topics are already above, in full
        for n in graph['nodes']:
            n.pop('is_seed', None)
        rels = []
        for l in index.search_links(col.id, query, 8)[:5]:
            with conn() as c:
                names = {r['id']: r['slug'] for r in c.execute('SELECT id, slug FROM library.topic WHERE id = ANY(%s)', ([l['from_topic'], l['to_topic']],)).fetchall()}
            rels.append({'from': names.get(l['from_topic']), 'relation': l['relation'], 'to': names.get(l['to_topic']), 'context': l['context']})
        blocks.append({**_head(col), 'answerable': True, 'topics': out, 'sources': _legend(col, legend), 'related': graph, 'relationships': rels})
    answerable = bool(blocks)
    return {'query': query, 'answerable': answerable, 'results': blocks, 'meta': {'topics_found': sum(len(b['topics']) for b in blocks), 'total_tokens': total},
            'note': NOTE if answerable else 'The knowledge base does not cover this question. Say so; do not answer from your own knowledge.',
            'next': ('Answer from these pages. Exact quote of a fact: library_topic_claims. A detail no page carries: library_source_search, then library_source. '
                     'Applying a rule to the data: plan_query with the entities in a topic\'s `model` list.') if answerable else
                    ('Before saying the documents do not cover it: if the question uses a company-specific word, describe(word) gives its meaning and other '
                     'names, then try library_search with those words once.')}


@_safe
def library_search(query: Annotated[str, Field(description='What you want to find, as a question or keywords')],
                   collection: Annotated[str | None, Field(description='Limit to one collection (name, slug or id); omit to search all you may read')] = None,
                   limit: Annotated[int, Field(description='Most topics per collection', ge=1, le=20)] = 10) -> dict:
    """Lightweight topic discovery in the company documents: slugs, titles, summaries, scores and the single best matching chunk, no page body. Use it
    to see which topics exist before reading some with library_topic, or traversing from them. Cheaper than library_context but not enough to answer from:
    to answer a question, call library_context.
    Each result carries its collection's usage tag: `public` may be shared with customers; `internal` is for your understanding only, do not quote it to customers."""
    cols = [_pick(collection)] if collection else readable()
    blocks = []
    for col in cols:
        hits = _topic_hits(col, query, max(1, min(int(limit), 20)))
        if hits:
            kinds = bridge.kinds_of(col.id, [h['topic_id'] for h in hits])
            blocks.append({**_head(col), 'topics': [{'topic': h['slug'], 'title': h['title'], 'type': h['topic_type'], 'kind_of': kinds.get(h['topic_id'], []), 'summary': h['summary'],
                                                     'score': round(h['score'], 4), 'link': _link(col, h['slug']), 'model': _model_of(col, h['topic_id']),
                                                     'top_chunk': {'chunk_index': h['idx'], 'text': h['text']}} for h in hits]})
    return {'query': query, 'results': blocks, 'note': NOTE if blocks else 'Nothing in the collections you may read matches. Say so; do not guess.',
            'next': 'Read a topic with library_topic, or ask library_context with the question to get the full pages at once.' if blocks else 'Try library_context with the question as asked, or other words.'}


def _topic_or_error(col, topic):
    t = topics.find(col.id, topic)
    if not t:
        return None, {**_head(col), 'error': f'no topic {topic!r} in this collection (library_search finds topics)'}
    return t, None


@_safe
def library_topic(collection: Annotated[str, Field(description='Collection name, slug or id')],
                  topic: Annotated[str, Field(description='Topic slug, title or alias')]) -> dict:
    """One full topic page by slug: its Markdown (sources cited as [s1], [s2]..., listed in `sources`), type, summary, aliases, typed links in both directions, the
    source documents behind it and the data-model entities it is about. The atomic claims with their quotes are in library_topic_claims.
    Each result carries its collection's usage tag: `public` may be shared with customers; `internal` is for your understanding only, do not quote it to customers."""
    col = _pick(collection)
    t, err = _topic_or_error(col, topic)
    if err:
        return err
    with conn() as c:
        out = c.execute("SELECT l.relation, o.slug, o.title, l.context FROM library.topic_link l JOIN library.topic o ON o.id=l.to_topic AND o.collection_id=l.collection_id WHERE l.from_topic=%s AND l.collection_id=%s AND o.status='active' ORDER BY o.title", (t['id'], col.id)).fetchall()
        inc = c.execute("SELECT l.relation, o.slug, o.title, l.context FROM library.topic_link l JOIN library.topic o ON o.id=l.from_topic AND o.collection_id=l.collection_id WHERE l.to_topic=%s AND l.collection_id=%s AND o.status='active' ORDER BY o.title", (t['id'], col.id)).fetchall()
        aliases = [r['alias'] for r in c.execute('SELECT alias FROM library.topic_alias WHERE topic_id=%s AND collection_id=%s ORDER BY alias', (t['id'], col.id)).fetchall()]
        srcs = c.execute('SELECT s.id, s.name, s.title, ts.wiki_version FROM library.topic_source ts JOIN library.source s ON s.id=ts.source_id AND s.collection_id=ts.collection_id WHERE ts.topic_id=%s ORDER BY s.name', (t['id'],)).fetchall()
    ver, md = index.published(col.id, t['id'])
    legend = {'by_id': {}, 'list': []}
    md = _short_citations(col, md, legend)
    refs = legend['by_id']
    for r in srcs:                                                                      # sources behind the topic that the page does not cite get a reference too
        refs.setdefault(str(r['id']), f's{len(refs) + 1}')
    return {**_head(col), 'topic': t['slug'], 'title': t['title'], 'type': t['topic_type'], 'kind_of': bridge.kinds_of(col.id, [t['id']]).get(t['id'], []), 'summary': t['summary'],
            'link': _link(col, t['slug']), 'aliases': aliases, 'version': ver, 'markdown': md, 'needs_revision': t['needs_revision'], 'model': _model_of(col, t['id']),
            'sources': [{'ref': refs[str(r['id'])], 'source_id': str(r['id']), 'name': r['title'] or r['name'], 'wiki_version': r['wiki_version'], 'link': _source_link(col, r['id'])} for r in srcs],
            'related': [{'relation': r['relation'], 'topic': r['slug'], 'title': r['title'], 'why': r['context']} for r in out],
            'related_from': [{'relation': r['relation'], 'topic': r['slug'], 'title': r['title'], 'why': r['context']} for r in inc],
            'next': 'Exact quotes: library_topic_claims. Related topics: library_traverse. The documents behind it: library_topic_sources, then library_source.'}


@_safe
def library_topic_claims(collection: Annotated[str, Field(description='Collection name, slug or id')],
                         topic: Annotated[str, Field(description='Topic slug, title or alias')]) -> dict:
    """All active claims of a topic, each with its exact quote copied from the source wiki, the source, the wiki version the quote comes from and the wiki
    passage it sits in. Use it when you need provenance: "where does this fact come from?". Read the quoted page with library_source.
    Each result carries its collection's usage tag: `public` may be shared with customers; `internal` is for your understanding only, do not quote it to customers."""
    col = _pick(collection)
    t, err = _topic_or_error(col, topic)
    if err:
        return err
    with conn() as c:
        claims = c.execute("SELECT id, n, text, kind FROM library.claim WHERE topic_id=%s AND collection_id=%s AND status='active' ORDER BY n", (t['id'], col.id)).fetchall()
        ev = c.execute("SELECT cs.claim_id, cs.source_id, s.name, s.title, cs.quote, cs.wiki_version, cs.wiki_chunk FROM library.claim_source cs LEFT JOIN library.source s ON s.id=cs.source_id AND s.collection_id=cs.collection_id "
                       "WHERE cs.collection_id=%s AND cs.claim_id = ANY(%s) ORDER BY cs.id", (col.id, [x['id'] for x in claims])).fetchall()
    return {**_head(col), 'topic': t['slug'], 'title': t['title'], 'link': _link(col, t['slug']),
            'claims': [{'n': x['n'], 'claim': x['text'], 'type': x['kind'],
                        'sources': [{'source_id': str(e['source_id']) if e['source_id'] else None, 'name': (e['title'] or e['name']) if e['source_id'] else '(deleted source)',
                                     'wiki_version': e['wiki_version'], 'wiki_passage': e['wiki_chunk'], 'quote': e['quote'],
                                     **({'link': _source_link(col, e['source_id'])} if e['source_id'] else {})} for e in ev if e['claim_id'] == x['id']]} for x in claims],
            'next': 'To read a quote in context: library_source with its source_id and around = its wiki_passage.'}


@_safe
def library_topic_sources(collection: Annotated[str, Field(description='Collection name, slug or id')],
                          topic: Annotated[str, Field(description='Topic slug, title or alias')]) -> dict:
    """The source documents that contributed to a topic: name, kind, address, the version of each source wiki the topic was built from, and whether that
    wiki is still the approved one. Read a source's wiki with library_source.
    Each result carries its collection's usage tag: `public` may be shared with customers; `internal` is for your understanding only, do not quote it to customers."""
    col = _pick(collection)
    t, err = _topic_or_error(col, topic)
    if err:
        return err
    with conn() as c:
        rows = c.execute("SELECT s.id, s.name, s.title, s.kind, s.url, s.wiki_status, ts.wiki_version, (SELECT max(version) FROM library.wiki_version w WHERE w.source_id=s.id) AS latest "
                         "FROM library.topic_source ts JOIN library.source s ON s.id=ts.source_id AND s.collection_id=ts.collection_id WHERE ts.topic_id=%s ORDER BY s.name", (t['id'],)).fetchall()
    return {**_head(col), 'topic': t['slug'], 'title': t['title'],
            'sources': [{'source_id': str(r['id']), 'name': r['title'] or r['name'], 'kind': r['kind'], **({'url': r['url']} if r['url'] else {}), 'wiki_version': r['wiki_version'],
                         'wiki_is_current': r['wiki_status'] == 'approved' and r['wiki_version'] == r['latest'], 'link': _source_link(col, r['id'])} for r in rows]}


@_safe
def library_traverse(collection: Annotated[str, Field(description='Collection name, slug or id')],
                     topic: Annotated[str | list[str], Field(description='Topic slug, title or alias to start from, or a list of them')],
                     depth: Annotated[int, Field(description='How many links to follow (1 to 3)', ge=1, le=3)] = 1,
                     relations: Annotated[list[str] | None, Field(description='Only follow these relation types, e.g. ["depends_on", "part_of"]')] = None,
                     max_topics: Annotated[int, Field(description='Ceiling on discovered topics', ge=1, le=100)] = 20) -> dict:
    """BFS graph traversal from one or more topics over typed links, both directions: `topics` (with summaries) and `links` (typed edges with why they
    relate). Use it to see what a topic depends on, what it is part of, what replaces it. Links never leave the collection; no page text.
    Each result carries its collection's usage tag: `public` may be shared with customers; `internal` is for your understanding only, do not quote it to customers."""
    col = _pick(collection)
    refs = topic if isinstance(topic, list) else [topic]
    seeds = [x['id'] for x in (topics.find(col.id, r) for r in refs) if x]
    if not seeds:
        return {**_head(col), 'error': f'no topic {refs!r} in this collection'}
    g = _graph(col, seeds, depth, max_topics, relations)
    return {**_head(col), 'start': [n['topic'] for n in g['nodes'] if n['is_seed']], 'topics': g['nodes'], 'links': g['edges'], 'truncated': g['truncated']}


@_safe
def library_source_search(query: Annotated[str, Field(description='Concept or keywords')],
                          collection: Annotated[str | None, Field(description='Limit to one collection (name, slug or id); omit to search all you may read')] = None,
                          limit: Annotated[int, Field(description='Most documents per collection', ge=1, le=10)] = 5) -> dict:
    """Search the approved source wikis (the verified page written from each document) by meaning and words. Returns documents with the passage that
    matched. Use it after library_context, for a detail the topic pages do not carry, or to find which documents discuss something; then read one with
    library_source.
    Each result carries its collection's usage tag: `public` may be shared with customers; `internal` is for your understanding only, do not quote it to customers."""
    cols = [_pick(collection)] if collection else readable()
    k = max(1, min(int(limit), 10))
    blocks = []
    for col in cols:
        seen, out = set(), []
        for h in index.search_wiki_chunks(col.id, query, pool=k * 8):
            sid = str(h['source_id'])
            if sid in seen:
                continue
            seen.add(sid)
            out.append({'source_id': sid, 'name': h['title'] or h['name'], 'wiki_version': h['version'], 'passage': h['idx'], 'section': ' > '.join(h['heading_path'] or []) or None,
                        'matched': h['text'][:600], 'link': _source_link(col, sid)})
            if len(out) >= k:
                break
        if out:
            blocks.append({**_head(col), 'sources': out})
    return {'query': query, 'results': blocks, 'note': NOTE if blocks else 'No approved source wiki matches. Say so; do not guess.',
            'next': 'Read a match with library_source (source_id, query or around = its passage). Topic pages may already cover it: library_context.' if blocks else ''}


READ_CHARS = 12000                                                            # most text one library_source call returns


def _window(rows, hits, window, budget):
    """Passages for a read from rows {idx: row}: the hits whole first (best first), then their neighbours nearest first, while they fit the budget.
    -> (passages in order, neighbours left out). Only the first hit may be cut."""
    chosen, left = {}, budget

    def add(i, cut_ok=False):
        nonlocal left
        r = rows.get(i)
        if r is None or i in chosen or left <= 0:
            return
        text = r['text']
        if len(text) > left:
            if not cut_ok:
                return
            text = text[:left] + ' ...'
        left -= len(text)
        chosen[i] = {'passage': i, 'match': i in hits, 'section': ' > '.join(r.get('heading_path') or []) or None, **({'location': r['location']} if r.get('location') else {}),
                     'text': text, **({'cut': True} if text != r['text'] else {})}

    for n, h in enumerate(hits):
        add(h, cut_ok=n == 0)
    for d in range(1, window + 1):
        for h in hits:
            add(h - d)
            add(h + d)
    return [chosen[i] for i in sorted(chosen)], sorted(i for i in rows if i not in chosen)


@_safe
def library_source(collection: Annotated[str, Field(description='Collection name, slug or id')],
                   source: Annotated[str, Field(description='The document: its `source_id` (from library_topic_sources, library_topic_claims or library_source_search) or its exact file name')],
                   query: Annotated[str | None, Field(description='Search inside this document for a question or keywords')] = None,
                   around: Annotated[int | None, Field(description='Read the passages around this passage number (a `wiki_passage` of a claim, or a `passage` of a search hit)', ge=0)] = None,
                   window: Annotated[int, Field(description='With `query` or `around`: passages to add on each side of each match (nearest first, as many as fit). Keep 1: tables and lists often continue in the next passage', ge=0, le=3)] = 1,
                   original: Annotated[bool, Field(description='Read the original extracted text of the document instead of its verified wiki (rarely needed)')] = False) -> dict:
    """Read a source document. By default its approved wiki: the verified, standalone page written from the document, which topics and claims quote.
    With neither `query` nor `around` you get the whole wiki page; `query` searches inside it (by meaning and words) and returns the best passages with
    their neighbours; `around` reads a passage and its neighbours. `original=true` reads the document's original extracted text instead (unreviewed).
    Each result carries its collection's usage tag: `public` may be shared with customers; `internal` is for your understanding only, do not quote it to customers."""
    col = _pick(collection)
    ref = (source or '').strip()
    with conn() as c:
        src = c.execute('SELECT id, name, title, kind, url, wiki_status FROM library.source WHERE collection_id=%s AND (id::text=%s OR lower(name)=lower(%s)) ORDER BY created_at DESC LIMIT 1', (col.id, ref, ref)).fetchone()
    if not src:
        return {**_head(col), 'error': f'no document {ref!r} in this collection (library_topic_sources names the documents behind a topic)'}
    sid = str(src['id'])
    head = {**_head(col), 'source_id': sid, 'name': src['title'] or src['name'], 'link': _source_link(col, sid), **({'url': src['url']} if src['url'] else {})}
    if original:
        with conn() as c:
            total = c.execute('SELECT count(*) AS n FROM library.source_chunk WHERE collection_id=%s AND source_id=%s', (col.id, sid)).fetchone()['n']
            hits = [h['chunk_index'] for h in retrieval.search(col, query, 3, source_id=sid)] if query and query.strip() else ([around] if around is not None else [])
            if not hits:
                rows = c.execute('SELECT idx, location, heading_path, left(text, 120) AS start FROM library.source_chunk WHERE collection_id=%s AND source_id=%s ORDER BY idx LIMIT 150', (col.id, sid)).fetchall()
                return {**head, 'view': 'original', 'passages_in_document': total, 'note': 'Original extracted text, not reviewed.',
                        'outline': [{'passage': r['idx'], 'section': ' > '.join(r['heading_path'] or []) or None, 'starts': r['start']} for r in rows]}
            want = sorted({i for h in hits for i in range(max(0, h - window), h + window + 1)})
            rows = {r['idx']: r for r in c.execute('SELECT idx, location, heading_path, text FROM library.source_chunk WHERE collection_id=%s AND source_id=%s AND idx = ANY(%s)', (col.id, sid, want)).fetchall()}
        passages, left = _window(rows, hits, window, READ_CHARS)
        return {**head, 'view': 'original', 'passages_in_document': total, 'passages': passages, 'not_included': left, 'note': 'Original extracted text, not reviewed.'}
    ver, md = index.approved_wiki(col.id, sid)
    if md is None:
        return {**head, 'error': 'this document has no approved wiki yet; read its original text with original=true'}
    head = {**head, 'view': 'wiki', 'wiki_version': ver}
    if not (query and query.strip()) and around is None:
        cut = len(md) > READ_CHARS * 2
        return {**head, 'markdown': md[:READ_CHARS * 2] + ('\n\n[... the page continues: search inside it with `query`, or read on with `around`]' if cut else ''), 'truncated': cut}
    with conn() as c:
        total = c.execute('SELECT count(*) AS n FROM library.wiki_chunk WHERE collection_id=%s AND source_id=%s AND version=%s', (col.id, sid, ver)).fetchone()['n']
    if query and query.strip():
        hits = [h['idx'] for h in index.search_wiki_chunks(col.id, query, pool=30) if str(h['source_id']) == sid][:3]
    else:
        hits = [around]
    if not hits:
        return {**head, 'passages_in_page': total, 'passages': [], 'note': 'Nothing in this page matches; try other words, or read the whole page without `query`.'}
    want = sorted({i for h in hits for i in range(max(0, h - window), h + window + 1)})
    with conn() as c:
        rows = {r['idx']: r for r in c.execute('SELECT idx, heading_path, text FROM library.wiki_chunk WHERE collection_id=%s AND source_id=%s AND version=%s AND idx = ANY(%s)', (col.id, sid, ver, want)).fetchall()}
    if not rows:
        return {**head, 'error': f'the page has passages 0 to {max(total - 1, 0)}'}
    passages, left = _window(rows, hits, window, READ_CHARS)
    return {**head, 'passages_in_page': total, 'passages': passages, 'not_included': left}


@_safe
def library_vocabulary(collection: Annotated[str | None, Field(description='Collection name, slug or id; omit for all you may read')] = None) -> dict:
    """The topic types and relation types (with their meaning and inverse) of a collection: the vocabulary its topics and links use.
    Each result carries its collection's usage tag: `public` may be shared with customers; `internal` is for your understanding only, do not quote it to customers."""
    cols = [_pick(collection)] if collection else readable()
    out = []
    for col in cols:
        v = vocab.get(col.id)
        out.append({**_head(col), 'topic_types': [{'name': r['name'], 'description': r['description'], 'status': r['status']} for r in v['topic_types']],
                    'relation_types': [{'name': r['name'], 'inverse': r['inverse_name'], 'description': r['description'], 'status': r['status']} for r in v['relation_types']]})
    return {'results': out}


LIBRARY = {'library_context': library_context, 'library_search': library_search, 'library_topic': library_topic, 'library_topic_claims': library_topic_claims,
           'library_topic_sources': library_topic_sources, 'library_traverse': library_traverse, 'library_source_search': library_source_search,
           'library_source': library_source, 'library_vocabulary': library_vocabulary, 'library_collections': library_collections}


def related_documents(entity_key, limit=5):
    """For the model tools: the topics, in collections this agent may read, that are about an entity. Empty when the agent has no document access."""
    try:
        p = _principal()
        cols = {c.id for c in readable(p)}
    except RuntimeError:
        return []
    blocks = [b for b in bridge.documents_for(p[0], 'entity', entity_key, limit) if b['collection_id'] in cols]
    for b in blocks:
        b['collection'] = b['collection']
    return blocks


def _brief(col, t, why):
    return {'topic': t['slug'], 'title': t['title'], 'type': t['topic_type'] if 'topic_type' in t else t.get('type'), 'summary': (t.get('summary') or '')[:200], 'why': why, 'link': _link(col, t['slug'])}


def documents_for_question(question, entity_keys, cap=25):
    """For plan_query: the topics, in collections this agent may read, that are about the entities of the question or that match its words. Brief entries (title, a line of
    summary, why it matched, where to open it): the agent opens what it needs with library_topic. Not cut to a handful: relevance decides, with a high cap that says how many more there are."""
    try:
        p = _principal()
        cols = readable(p)
    except RuntimeError:
        return []
    names = {}
    try:
        onto = bridge._onto() or {}
        names = {k: e['name'] for k, e in onto.get('entities', {}).items()}
    except Exception:
        pass
    blocks, shown, more = [], 0, 0
    for col in cols:
        found = {}
        with conn() as c:
            linked = c.execute("SELECT t.slug, t.title, t.topic_type, t.summary, r.ref FROM library.topic_model_ref r JOIN library.topic t ON t.id=r.topic_id AND t.collection_id=r.collection_id "
                               "WHERE r.collection_id=%s AND r.status='confirmed' AND r.kind='entity' AND r.ref = ANY(%s) AND t.status='active' ORDER BY t.title", (col.id, list(entity_keys))).fetchall()
        for t in linked:
            found.setdefault(t['slug'], _brief(col, t, []))['why'].append(f"is about {names.get(t['ref'], t['ref'])}")
        for t in _topic_hits(col, question, 10):
            e = found.setdefault(t['slug'], {'topic': t['slug'], 'title': t['title'], 'type': t['topic_type'], 'summary': (t['summary'] or '')[:200], 'why': [], 'link': _link(col, t['slug'])})
            e['why'].append('matches the question')
        items = list(found.values())
        room = max(0, cap - shown)
        more += max(0, len(items) - room)
        items = items[:room]
        shown += len(items)
        if items:
            blocks.append({**_head(col), 'topics': [{**i, 'why': '; '.join(i['why'])} for i in items]})
    if more and blocks:
        blocks[-1]['more_topics'] = f'{more} more matched; use library_search to see them'
    return blocks


def lookup_name(name, limit=5):
    """For describe: a word the model does not know (an abbreviation, a product name) found in the documents this agent may read, with the model entities each topic is about."""
    try:
        p = _principal()
        cols = readable(p)
    except RuntimeError:
        return []
    blocks = []
    for col in cols:
        t = topics.find(col.id, name)
        rows = [t] if t else topics.search(col.id, name, limit)
        out = []
        for r in rows:
            slug = r['slug']
            tt = topics.find(col.id, slug)
            if not tt:
                continue
            out.append({'topic': tt['slug'], 'title': tt['title'], 'type': tt['topic_type'], 'summary': (tt['summary'] or '')[:300], 'exact': bool(t), 'model': _model_of(col, tt['id']), 'link': _link(col, tt['slug'])})
        if out:
            blocks.append({**_head(col), 'topics': out})
    return blocks
