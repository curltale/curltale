"""The topic wiki of a collection.

When a source wiki is approved, the analyzer (with search_topics / get_topic) decides which topics it creates, updates or complements and extracts
atomic claims, each with a quote copied verbatim from the wiki. The topic writer then writes or amends each topic page from the FULL verified source
wiki (not only the claims), with tools to read related topics and other approved source wikis, citing sources as `[source: <id>]`. Claims are stored
with the wiki version (and the wiki chunk) their quote comes from; a source's old claims are superseded, never deleted. A topic this source no longer
covers gets a surgical rewrite that audits each withdrawn claim against the remaining sources. Every page version is chunked and embedded; the topic's
identity vector is rebuilt from its claims; typed links are embedded too (see index.py).

Stricter than the analyzer's own output: a claim whose quote is not in the wiki is dropped (it is never given the summary as a quote); an existing page is
extended even when the analyzer says "create"; everything happens inside one collection (tools, searches and writes all carry its id)."""
import json
import re
from pathlib import Path
from .. import config, embeddings, llm, settings, vectors
from ..db import conn, J
from . import index, retrieval, scope as S, vocab, wiki

PROMPTS = Path(__file__).parent / 'prompts'
MAX_TOPICS, MAX_CLAIMS = 12, 80                    # at most 12 topics per source
KINDS = ('fact', 'definition', 'rule', 'step', 'limitation')


def slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')[:80] or 'topic'


def norm(text):
    return re.sub(r'\s+', ' ', re.sub(r'[*_`#>\[\]]', '', text or '')).strip().casefold()


# ---------------------------------------------------------------- finding topics
def find(cid, ref, drafts=False, follow=True):
    """A topic of this collection by slug, title or alias (exact); an old address of a merged topic leads to the topic that took it over."""
    states = ('active', 'draft') if drafts else ('active',)
    with conn() as c:
        t = c.execute("SELECT t.* FROM library.topic t WHERE t.collection_id=%s AND t.status = ANY(%s) AND (t.slug=%s OR lower(t.title)=lower(%s) OR EXISTS "
                      "(SELECT 1 FROM library.topic_alias a WHERE a.topic_id=t.id AND lower(a.alias)=lower(%s))) LIMIT 1", (cid, list(states), slugify(ref), ref, ref)).fetchone()
        if not t and follow:
            t = c.execute("SELECT t.* FROM library.topic_redirect r JOIN library.topic t ON t.id=r.to_topic AND t.collection_id=r.collection_id WHERE r.collection_id=%s AND r.from_slug=%s AND t.status='active'", (cid, slugify(ref))).fetchone()
    return t


def search(cid, query, k=6):
    """Topics of this collection for a question: by meaning when vectors exist, and by words in title, aliases and summary."""
    query = (query or '').strip()
    if not query:
        return []
    cast = vectors.spec(config.EMBEDDING_DIM)['cast']
    cols = "t.slug, t.title, t.topic_type, t.summary"
    out = {}
    with conn() as c:
        if embeddings.available() and c.execute('SELECT 1 FROM library.topic WHERE collection_id=%s AND embedding IS NOT NULL LIMIT 1', (cid,)).fetchone():
            try:
                q = embeddings.vec_literal(embeddings.embed_query(query))
                c.execute('SET LOCAL hnsw.iterative_scan = strict_order')
                for r in c.execute(f"SELECT {cols}, 1 - (t.embedding <=> %s::{cast}) AS sim FROM library.topic t WHERE t.collection_id=%s AND t.status='active' AND t.embedding IS NOT NULL "
                                   f"ORDER BY t.embedding <=> %s::{cast} LIMIT %s", (q, cid, q, k)).fetchall():
                    if r['sim'] >= settings.get('LIBRARY_MIN_SIMILARITY'):
                        out[r['slug']] = {**r, 'similarity': round(r['sim'], 3)}
            except RuntimeError:
                pass
        words = [w for w in re.findall(r'\w{3,}', query.lower())][:6]
        if words:
            like = ' OR '.join(['(lower(t.title) LIKE %s OR lower(t.summary) LIKE %s OR EXISTS (SELECT 1 FROM library.topic_alias a WHERE a.topic_id=t.id AND lower(a.alias) LIKE %s))'] * len(words))
            args = [x for w in words for x in (f'%{w}%',) * 3]
            for r in c.execute(f"SELECT {cols} FROM library.topic t WHERE t.collection_id=%s AND t.status='active' AND ({like}) LIMIT %s", (cid, *args, k)).fetchall():
                out.setdefault(r['slug'], {**r, 'similarity': None})
    for r in out.values():
        r.pop('sim', None)
    return sorted(out.values(), key=lambda r: -(r['similarity'] or 0))[:k]


def current_markdown(topic_id):
    with conn() as c:
        r = c.execute("SELECT markdown FROM library.topic_version WHERE topic_id=%s AND status='published' ORDER BY version DESC LIMIT 1", (topic_id,)).fetchone()
    return r['markdown'] if r else ''


TOOLS = [
    {'type': 'function', 'function': {'name': 'search_topics', 'description': 'Semantic search over existing topic pages. Call this for each candidate topic you identify to find any existing topics that may overlap, be the same as, or be closely related to your candidate.',
                                      'parameters': {'type': 'object', 'properties': {'query': {'type': 'string', 'description': 'Natural language query describing the topic to search for.'},
                                                                                      'limit': {'type': 'integer', 'description': 'Maximum results (1-8). Defaults to 5.', 'default': 5}}, 'required': ['query']}}},
    {'type': 'function', 'function': {'name': 'get_topic', 'description': 'Fetch the full current content of an existing topic page by slug. Use this after search_topics to read the full page and judge whether the new source evidence should update it, complement it, or create a separate topic.',
                                      'parameters': {'type': 'object', 'properties': {'slug': {'type': 'string', 'description': 'Topic slug as returned by search_topics.'}}, 'required': ['slug']}}},
]
WRITER_TOOLS = [
    {'type': 'function', 'function': {'name': 'search_topics', 'description': 'Semantic search over existing topic pages. Use this to find related topics you plan to link to, verify that link targets exist, or discover additional context before writing relationship sentences.',
                                      'parameters': {'type': 'object', 'properties': {'query': {'type': 'string', 'description': 'Natural language query.'},
                                                                                      'limit': {'type': 'integer', 'description': 'Maximum results (1-8). Defaults to 5.', 'default': 5}}, 'required': ['query']}}},
    {'type': 'function', 'function': {'name': 'get_topic', 'description': 'Fetch the full current content of an existing topic page by slug. Use this before writing a relationship sentence so you can describe the link accurately.',
                                      'parameters': {'type': 'object', 'properties': {'slug': {'type': 'string', 'description': 'Topic slug as returned by search_topics.'}}, 'required': ['slug']}}},
    {'type': 'function', 'function': {'name': 'search_source_wikis', 'description': 'Search for other approved source wiki pages in the knowledge base by concept or keyword. Use this to find additional evidence beyond the source provided in the task. Returns source_id, title, and URL for each match.',
                                      'parameters': {'type': 'object', 'properties': {'query': {'type': 'string', 'description': 'Natural language or keyword query.'},
                                                                                      'limit': {'type': 'integer', 'description': 'Maximum results (1-8). Defaults to 5.', 'default': 5}}, 'required': ['query']}}},
    {'type': 'function', 'function': {'name': 'get_source_wiki', 'description': 'Fetch the full approved wiki markdown for a source by its source_id. Use this after search_source_wikis to read a relevant source in full.',
                                      'parameters': {'type': 'object', 'properties': {'source_id': {'type': 'string', 'description': 'Source ID as returned by search_source_wikis.'}}, 'required': ['source_id']}}},
]


class TopicTools:
    """Tools a model may use on the topics of ONE collection (the analyzer's)."""

    def __init__(self, cid):
        self.cid = str(cid)

    def __call__(self, name, args):
        if name == 'search_topics':
            hits = search(self.cid, str(args.get('query', '')), max(1, min(int(args.get('limit') or 5), 8)))
            return json.dumps({'topics': [{'slug': h['slug'], 'title': h['title'], 'topic_type': h['topic_type'], 'summary': h['summary'], 'score': h['similarity']} for h in hits]})
        if name == 'get_topic':
            t = find(self.cid, str(args.get('slug', '')))
            return json.dumps({'slug': t['slug'], 'title': t['title'], 'topic_type': t['topic_type'], 'summary': t['summary'], 'markdown': current_markdown(t['id'])} if t else {'error': f"topic not found: {args.get('slug', '')}"})
        raise ValueError(f'unknown tool {name}')


class TopicWriterTools(TopicTools):
    """The topic writer's tools: topics, plus the approved source wikis of the same collection (search_source_wikis / get_source_wiki)."""

    def __init__(self, cid, current_sid=None):
        super().__init__(cid)
        self.current = str(current_sid) if current_sid else None

    def __call__(self, name, args):
        if name == 'search_source_wikis':
            limit = max(1, min(int(args.get('limit') or 5), 8))
            seen, out = set(), []
            for h in index.search_wiki_chunks(self.cid, str(args.get('query', '')), pool=40, exclude_source=self.current):
                sid = str(h['source_id'])
                if sid not in seen:
                    seen.add(sid)
                    out.append({'source_id': sid, 'title': h['title'] or h['name'], 'url': h['url'] or h['name'], 'matched': h['text'][:300]})
                if len(out) >= limit:
                    break
            return json.dumps({'sources': out})
        if name == 'get_source_wiki':
            sid = str(args.get('source_id', '')).strip()
            if not sid:
                return json.dumps({'error': 'source_id is required'})
            if sid == self.current:
                return json.dumps({'error': 'the current source wiki is already provided in the task context'})
            if not re.fullmatch(r'[0-9a-f-]{36}', sid):
                return json.dumps({'error': f'no approved wiki found for source: {sid}'})
            ver, md = index.approved_wiki(self.cid, sid)
            if md is None:
                return json.dumps({'error': f'no approved wiki found for source: {sid}'})
            with conn() as c:
                r = c.execute('SELECT name, title FROM library.source WHERE id=%s AND collection_id=%s', (sid, self.cid)).fetchone()
            return json.dumps({'source_id': sid, 'title': (r['title'] or r['name']) if r else '', 'markdown': md})
        return super().__call__(name, args)


# ---------------------------------------------------------------- analyzing
def _vocab_text(cid):
    """The ontology as the analyzer receives it: `- name — description` (pending ones marked)."""
    v = vocab.get(cid)
    fmt = lambda rows: '\n'.join(f"- {r['name']}{' (pending)' if r['status'] == 'pending' else ''}" + (f" — {r['description']}" if r.get('description') else '') for r in rows)
    return fmt(v['topic_types']) or '- Topic', fmt(v['relation_types']) or '- related_to'


def _template(cid, topic_type):
    with conn() as c:
        r = c.execute('SELECT template FROM library.topic_type WHERE collection_id=%s AND name=%s', (cid, topic_type)).fetchone()
    return (r['template'] if r else '') or ''


def _entities_text(markdown):
    """The model's entities as the analyzer sees them (structure only: name, domain, a short definition). A very large model is narrowed to the likely ones."""
    from . import bridge
    cat = bridge.entity_catalog(markdown[:2500])
    if not cat:
        return '(there is no published data model: leave model_links empty)'
    return '\n'.join(f"{k} | {e['name']} | {e.get('domain') or ''} | {(e.get('definition') or '')[:140]}" for k, e in cat.items())


def analyze(cid, markdown, title):
    """The topic analyzer, with the collection's ontology and the data model injected; it may search and read existing topics first."""
    tt, rt = _vocab_text(cid)
    system = (PROMPTS / 'analyzer.md').read_text().replace('{{topic_types}}', tt).replace('{{relation_types}}', rt).replace('{{entities}}', _entities_text(markdown))
    msgs = [{'role': 'system', 'content': system}, {'role': 'user', 'content': f'Source title: {title or ""}\n\nVerified source wiki:\n\n{markdown}'}]
    for attempt in range(2):
        text, _ = llm.chat_tools(msgs, TOOLS, TopicTools(cid), role='analyzer', max_rounds=20, max_tokens=12000)
        data = wiki._json_object(text)
        if isinstance(data, dict) and isinstance(data.get('topics'), list):
            return data
        msgs = msgs + [{'role': 'assistant', 'content': text or ''}, {'role': 'user', 'content': 'You have gathered sufficient evidence. Output your final JSON analysis now.'}]
    return None                                                                       # normalize() falls back to one topic from the page


def _title_from_markdown(markdown, fallback='Knowledge Topic'):
    for line in (markdown or '').splitlines():
        m = re.match(r'^#\s+(.+)$', line.strip())
        if m:
            return m.group(1).strip()[:160]
    return (fallback or 'Knowledge Topic')[:160]


def _first_paragraph(markdown):
    paras = [p.strip() for p in re.split(r'\n\s*\n', markdown or '') if p.strip()]
    for p in paras:
        if not p.startswith('#'):
            return p[:700]
    return (markdown or '').strip()[:700]


def normalize(data, markdown, fallback_title='Knowledge Topic'):
    """Keep only what can be trusted: claims whose quote really is in the source wiki, known actions, bounded sizes. No usable analysis: one topic from
    the page's title and first paragraph."""
    page = norm(markdown)
    raw_topics = (data or {}).get('topics') if isinstance(data, dict) else None
    if not isinstance(raw_topics, list) or not raw_topics:
        title, para = _title_from_markdown(markdown, fallback_title), _first_paragraph(markdown)
        raw_topics = [{'title': title, 'slug': slugify(title), 'topic_type': 'Topic', 'summary': para, 'aliases': [], 'action': 'create',
                       'claims': [{'claim_text': para, 'claim_type': 'fact', 'source_quote': para}] if para else [], 'links': []}]
        data = {'topics': raw_topics}
    topics = []
    for t in raw_topics[:MAX_TOPICS]:
        if not isinstance(t, dict):
            continue
        title = str(t.get('title') or '').strip() or fallback_title
        summary = str(t.get('summary') or '').strip() or _first_paragraph(markdown)
        claims, seen = [], set()
        for cl in (t.get('claims') or [])[:MAX_CLAIMS]:
            text, quote = str((cl or {}).get('claim_text', '')).strip(), str((cl or {}).get('source_quote', '')).strip()
            if not text or not quote or norm(quote) not in page or norm(text) in seen:
                continue                                                                  # a claim that cannot be traced to the wiki is dropped
            seen.add(norm(text))
            claims.append({'text': text[:1000], 'kind': cl.get('claim_type') if cl.get('claim_type') in KINDS else 'fact', 'quote': quote[:1000]})
        topics.append({'title': title[:160], 'slug': slugify(t.get('slug') or title), 'action': t.get('action') if t.get('action') in ('create', 'update', 'complement') else 'create',
                       'existing_slug': str(t.get('existing_slug') or '').strip()[:120], 'topic_type': str(t.get('topic_type') or 'Topic').strip()[:80] or 'Topic',
                       'fallback': str(t.get('topic_type_fallback') or '').strip()[:80], 'summary': summary[:900],
                       'aliases': [str(a).strip()[:160] for a in (t.get('aliases') or []) if str(a).strip()][:20], 'claims': claims,
                       'conflict_claim_text': str(t.get('conflict_claim_text') or '').strip()[:1000], 'conflict_reason': str(t.get('conflict_reason') or '').strip()[:500],
                       'model_links': [m for m in t.get('model_links') or [] if isinstance(m, dict)][:12],
                       'links': [{'target': str(l.get('target_title', '')).strip()[:160], 'relation': str(l.get('relation_type') or 'related_to').strip()[:80] or 'related_to', 'context': str(l.get('context') or '')[:500]}
                                 for l in (t.get('links') or []) if isinstance(l, dict) and str(l.get('target_title', '')).strip()][:20]})
    proposals = []
    for p in (data.get('ontology_proposals') or []):                                   # the analyzer's shape
        if isinstance(p, dict) and p.get('proposal_type') in ('topic_type', 'relation_type'):
            pay = p.get('payload') if isinstance(p.get('payload'), dict) else {}
            if str(pay.get('name', '')).strip():
                proposals.append({'kind': p['proposal_type'], 'name': str(pay['name']).strip()[:80], 'description': str(pay.get('description') or '')[:500], 'reason': str(p.get('reason') or '')[:1000]})
    for p in (data.get('vocabulary_proposals') or []):                                 # the older shape, still accepted
        if isinstance(p, dict) and p.get('kind') in ('topic_type', 'relation_type') and str(p.get('name', '')).strip():
            proposals.append({'kind': p['kind'], 'name': str(p['name']).strip()[:80], 'description': str(p.get('description') or '')[:500], 'reason': str(p.get('reason') or '')[:1000]})
    return {'topics': topics, 'proposals': proposals[:20]}


# ---------------------------------------------------------------- writing topic pages
def _fallback_page(topic, sid):
    facts = '\n'.join(f"- {c['text']} `[source: {sid}]`" for c in (topic.get('claims') or [])[:8])
    links = '\n'.join(f"- {l['target']} ({l['relation']})" for l in (topic.get('links') or [])[:12])
    return (f"# {topic['title']}\n\n## Overview\n\n{topic.get('summary') or ''}\n\n## Key Facts\n\n{facts or '- No atomic facts recorded yet.'}\n\n"
            f"## Related Topics\n\n{links or '- No related topics recorded yet.'}\n\n## Sources\n\n" + (f"- `{sid}`\n" if sid else ''))


def _candidate_json(topic):
    """The analyzer's candidate as the writer receives it."""
    return {'title': topic['title'], 'slug': topic.get('slug', ''), 'topic_type': topic.get('topic_type', 'Topic'), 'summary': topic.get('summary', ''), 'aliases': topic.get('aliases', []),
            'action': topic.get('action', 'create'), 'claims': [{'claim_text': c['text'], 'claim_type': c.get('kind', 'fact'), 'source_quote': c.get('quote', '')} for c in topic.get('claims') or []],
            'links': [{'target_title': l['target'], 'relation_type': l['relation'], 'context': l.get('context', '')} for l in topic.get('links') or []]}


def write_topic_markdown(cid, sid, topic, source_markdown, existing_markdown='', old_claims=None):
    """The topic page, written (or amended) from the candidate and the full verified source wiki, with tools to read related topics and other approved
    source wikis of the collection. Output that is not a Markdown page falls back to a plain page built from the candidate."""
    parts = []
    if existing_markdown:
        parts.append(f'Existing topic page (enrich and amend this — do not drop cited facts unless the new evidence supersedes them):\n\n{existing_markdown}\n\n---')
    if old_claims:
        block = '\n'.join(f"- claim: {c['text']}\n  quote: {c.get('quote') or '(no quote)'}" for c in old_claims)
        parts.append(f'Claims from the previous version of this source that are being replaced (for context on what changed):\n\n{block}\n\n---')
    parts.append(f"Source id: {sid}\n\nTopic candidate JSON:\n{json.dumps(_candidate_json(topic), indent=2, ensure_ascii=False)}\n\nVerified source wiki evidence:\n\n{source_markdown}")
    system = (PROMPTS / 'topic_writer.md').read_text().replace('{{title}}', topic['title']).replace('{{type_template}}', _template(cid, topic.get('topic_type') or ''))
    msgs = [{'role': 'system', 'content': system}, {'role': 'user', 'content': '\n\n'.join(parts)}]
    try:
        text, _ = llm.chat_tools(msgs, WRITER_TOOLS, TopicWriterTools(cid, sid), role='topic_writer', max_rounds=12, max_tokens=12000, temperature=0.2)
    except Exception:
        text = ''
    md = (text or '').strip().lstrip('﻿')
    if not md.startswith('#'):
        md = wiki._strip_preamble(md)
    return md if md.startswith('#') else _fallback_page(topic, sid)


def surgical_rewrite(cid, tid, superseded, current_sid=None):
    """The surgical rewrite: audit each withdrawn claim against the remaining sources (with tools) and remove or soften only the passages
    nothing else supports. -> the revised page, or None when the writer did not return a Markdown page."""
    md = current_markdown(tid)
    if not md or not superseded:
        return None
    block = '\n'.join(f"- claim: {c['text']}\n  quote: {c.get('quote') or '(no quote)'}" for c in superseded)
    msgs = [{'role': 'system', 'content': (PROMPTS / 'topic_surgical.md').read_text()},
            {'role': 'user', 'content': f'Current topic page:\n\n{md}\n\n---\n\nClaims that are no longer attributed to any remaining source and must be reviewed:\n\n{block}'}]
    text, _ = llm.chat_tools(msgs, WRITER_TOOLS, TopicWriterTools(cid, current_sid), role='topic_writer', max_rounds=12, max_tokens=12000, temperature=0)
    out = (text or '').strip().lstrip('﻿')
    return out if out.startswith('#') else None


# ---------------------------------------------------------------- storing
def _unique_slug(c, cid, base):
    slug, n = base, 2
    while c.execute('SELECT 1 FROM library.topic WHERE collection_id=%s AND slug=%s', (cid, slug)).fetchone():
        slug = f'{base}-{n}'; n += 1
    return slug


def store_version(cid, tid, markdown, reason, status='published'):
    """A new version of a topic page (published, or held for review), chunked and embedded. -> version number."""
    with conn() as c:
        ver = _version(c, tid)
        c.execute('INSERT INTO library.topic_version (collection_id,topic_id,version,markdown,update_reason,status) VALUES (%s,%s,%s,%s,%s,%s)', (cid, tid, ver, markdown, reason, status))
        if status == 'published':
            c.execute('UPDATE library.topic SET needs_revision=false, updated_at=now() WHERE id=%s', (tid,))
    index.index_topic(cid, tid, ver, markdown)
    return ver


def supersede_from_source(c, tid, sid):
    """This source no longer states its claims on a topic: a claim only it supported is superseded (kept, with its quote, for the surgical rewrite);
    a claim another source also supports stays active and only loses this source's quote. -> the superseded claims (with their quote)."""
    rows = c.execute("SELECT DISTINCT cl.id, cl.n, cl.text, cs.quote, EXISTS (SELECT 1 FROM library.claim_source o WHERE o.claim_id=cl.id AND o.source_id IS NOT NULL AND o.source_id<>%s) AS shared "
                     "FROM library.claim cl JOIN library.claim_source cs ON cs.claim_id=cl.id AND cs.source_id=%s WHERE cl.topic_id=%s AND cl.status='active'", (sid, sid, tid)).fetchall()
    only = [r for r in rows if not r['shared']]
    if only:
        c.execute("UPDATE library.claim SET status='superseded', superseded_at=now() WHERE id = ANY(%s)", ([r['id'] for r in only],))
    shared = [r['id'] for r in rows if r['shared']]
    if shared:
        c.execute('DELETE FROM library.claim_source WHERE source_id=%s AND claim_id = ANY(%s)', (sid, shared))
    return only


def active_claims_from_source(c, tid, sid):
    return c.execute("SELECT DISTINCT cl.n, cl.text, cs.quote FROM library.claim cl JOIN library.claim_source cs ON cs.claim_id=cl.id AND cs.source_id=%s "
                     "WHERE cl.topic_id=%s AND cl.status='active' ORDER BY cl.n", (sid, tid)).fetchall()


def insert_claims(c, cid, tid, sid, wiki_version, claims, status='active'):
    """Store the source's claims on the topic with their quote and the wiki version (and chunk) the quote is in. A claim the topic already states (same
    words) is re-attached rather than duplicated, so its number stays stable. -> the claim numbers."""
    out = []
    existing = {norm(r["text"]): r for r in c.execute("SELECT id, n, text, status FROM library.claim WHERE topic_id=%s AND status<>'rejected'", (tid,)).fetchall()}
    for cl in claims:
        hit = existing.get(norm(cl['text']))
        if hit:
            claim_id, n = hit['id'], hit['n']
            if hit['status'] != 'active':
                c.execute('UPDATE library.claim SET status=%s, superseded_at=NULL, kind=%s WHERE id=%s', (status, cl['kind'], claim_id))
            c.execute('DELETE FROM library.claim_source WHERE claim_id=%s AND source_id=%s', (claim_id, sid))
        else:
            n = c.execute('SELECT coalesce(max(n),0)+1 AS n FROM library.claim WHERE topic_id=%s', (tid,)).fetchone()['n']
            claim_id = c.execute('INSERT INTO library.claim (collection_id,topic_id,n,text,kind,status) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id', (cid, tid, n, cl['text'], cl['kind'], status)).fetchone()['id']
            existing[norm(cl['text'])] = {'id': claim_id, 'n': n, 'status': status}
        c.execute('INSERT INTO library.claim_source (collection_id,claim_id,source_id,quote,wiki_version,wiki_chunk) VALUES (%s,%s,%s,%s,%s,%s)',
                  (cid, claim_id, sid, cl['quote'], wiki_version, index.quote_place(cid, sid, wiki_version, cl['quote']) if wiki_version else None))
        out.append(n)
    return out


def nearest(cid, title, aliases, summary, claims=()):
    """The existing active topic most like a candidate (by meaning): (row, similarity) or (None, 0). The candidate is embedded the way topics are
    (title, aliases, summary and claims), so the two vectors are comparable."""
    if not embeddings.available():
        return None, 0
    try:
        text = ' '.join((f"{title}\n\n{', '.join(aliases)}\n\n{summary}\n\n" + '\n'.join(c['text'] for c in claims)).split()[:3000])
        q = embeddings.vec_literal(embeddings.embed([text])[0])
    except RuntimeError:
        return None, 0
    cast = vectors.spec(config.EMBEDDING_DIM)['cast']
    with conn() as c:
        c.execute('SET LOCAL hnsw.iterative_scan = strict_order')
        r = c.execute(f"SELECT id, slug, title, 1 - (embedding <=> %s::{cast}) AS sim FROM library.topic WHERE collection_id=%s AND status='active' AND embedding IS NOT NULL ORDER BY embedding <=> %s::{cast} LIMIT 1", (q, cid, q)).fetchone()
    return (r, r['sim']) if r else (None, 0)


def _version(c, tid):
    return c.execute('SELECT coalesce(max(version),0)+1 AS v FROM library.topic_version WHERE topic_id=%s', (tid,)).fetchone()['v']


def _published_version(c, tid):
    r = c.execute("SELECT max(version) AS v FROM library.topic_version WHERE topic_id=%s AND status='published'", (tid,)).fetchone()
    return r['v']


def _wiki_version(cid, sid):
    with conn() as c:
        r = c.execute('SELECT max(version) AS v FROM library.wiki_version WHERE source_id=%s AND collection_id=%s', (sid, cid)).fetchone()
    return r['v'] if r else None


def run(cid, sid, markdown, title, ctx=None, user='system'):
    """Update the topic wiki of one collection from an approved source wiki. Changes that contradict the existing page, and new topics that look like
    an existing one, are written but held for a person (the review gate). Returns {'created', 'updated', 'held', 'proposals', 'revised'}."""
    step = ctx.progress if ctx else (lambda *a, **k: None)
    step('deciding which topics this source changes', 20)
    wiki_version = _wiki_version(cid, sid)
    analysis = normalize(analyze(cid, markdown, title), markdown, title or 'Knowledge Topic')
    with conn() as c:
        prev_topics = {str(r['topic_id']) for r in c.execute('SELECT topic_id FROM library.topic_source WHERE source_id=%s AND collection_id=%s', (sid, cid)).fetchall()}
    created, updated, held_list, by_title, touched, held_titles, new_topics = [], [], [], {}, [], set(), set()
    v = vocab.get(cid)
    approved_t = {r['name'] for r in v['topic_types'] if r['status'] == 'approved'}
    known_t = {r['name'] for r in v['topic_types']}
    approved_r = {r['name'] for r in v['relation_types'] if r['status'] == 'approved'}
    known_r = {r['name'] for r in v['relation_types']}
    proposals = list(analysis['proposals'])
    seen_p = {(p['kind'], p['name']) for p in proposals}
    threshold = settings.get('DUPLICATE_SIMILARITY')
    total = max(1, len(analysis['topics']))
    for i, t in enumerate(analysis['topics']):
        step(f"writing the topic '{t['title']}'", 30 + int(60 * i / total))
        ttype = t['topic_type']
        if ttype not in known_t:                                                       # unknown type: created as pending and proposed to the manager
            if ('topic_type', ttype) not in seen_p:
                proposals.append({'kind': 'topic_type', 'name': ttype, 'description': '', 'reason': f"New type encountered while processing '{title}'"})
                seen_p.add(('topic_type', ttype))
            known_t.add(ttype)
        fallback = None if ttype in approved_t else (t['fallback'] if t['fallback'] in approved_t else 'Topic')
        stored_type = ttype if ttype in approved_t else fallback                       # a pending type is used only once it is approved
        target = find(cid, t['existing_slug'], drafts=True) if t['existing_slug'] else None
        target = target or find(cid, t['slug'], drafts=True) or find(cid, t['title'], drafts=True)
        # --- the review gate: a contradiction of the existing page, or a new topic that is probably an existing one
        conflict = None
        if target:
            if t['conflict_claim_text']:
                with conn() as c:
                    hit = next((r for r in c.execute("SELECT n, text FROM library.claim WHERE topic_id=%s AND status='active'", (target['id'],)).fetchall() if norm(r['text']) == norm(t['conflict_claim_text'])), None)
                conflict = {'kind': 'contradiction', 'n': hit['n'] if hit else None, 'dup': None,
                            'text': t['conflict_reason'] or f"New evidence may contradict: \"{t['conflict_claim_text']}\""}
        else:
            dup, sim = nearest(cid, t['title'], t['aliases'], t['summary'], t['claims'])
            if dup and sim >= threshold:
                conflict = {'kind': 'duplicate_topic', 'n': None, 'dup': dup['id'], 'text': f"New topic \"{t['title']}\" is {sim:.0%} similar to existing topic \"{dup['title']}\" — likely the same subject."}
        held = conflict is not None
        with conn() as c:
            if target:
                tid, slug = target['id'], target['slug']
                existing_md = current_markdown(tid)                                     # an existing page is always extended, never overwritten
                base = _published_version(c, tid)
                if not held:
                    c.execute("UPDATE library.topic SET updated_at=now(), summary=CASE WHEN %s<>'' THEN %s ELSE summary END WHERE id=%s", (t['summary'], t['summary'], tid))
                is_new = False
            else:
                slug = _unique_slug(c, cid, t['slug'])
                tid = c.execute('INSERT INTO library.topic (collection_id,slug,title,topic_type,summary,status) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id',
                                (cid, slug, t['title'], stored_type, t['summary'], 'draft' if held else 'active')).fetchone()['id']
                existing_md, is_new, base = '', True, None
            for a in [t['title']] + t['aliases']:
                c.execute('INSERT INTO library.topic_alias (collection_id,topic_id,alias) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING', (cid, tid, a))
            if not held:                                                               # a held change's source counts once a person approves it
                c.execute('INSERT INTO library.topic_source (collection_id,topic_id,source_id,wiki_version) VALUES (%s,%s,%s,%s) ON CONFLICT (topic_id, source_id) DO UPDATE SET wiki_version=EXCLUDED.wiki_version',
                          (cid, tid, sid, wiki_version))
            old_claims = active_claims_from_source(c, tid, sid) if existing_md else []
        md = write_topic_markdown(cid, sid, {**t, 'slug': slug, 'topic_type': stored_type}, markdown, existing_md, old_claims=[dict(x) for x in old_claims] or None)
        reason = f"{'Created' if is_new else 'Updated'} from source {sid}" + (f" ({t['action']})" if not is_new else '')
        ver = store_version(cid, tid, md, reason, 'held' if held else 'published')
        with conn() as c:
            if not held:
                supersede_from_source(c, tid, sid)
            insert_claims(c, cid, tid, sid, wiki_version, t['claims'], 'pending' if held else 'active')
            if held:
                c.execute('INSERT INTO library.topic_conflict (collection_id,topic_id,version,base_version,kind,description,contradicts_n,duplicate_of,source_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                          (cid, tid, ver, base, conflict['kind'], conflict['text'][:1000], conflict['n'], conflict['dup'], sid))
        if not held:
            index.embed_topic(cid, tid)                                                 # identity rebuilt with the claims (held drafts do not move it)
        by_title[t['title'].lower()] = tid
        by_title[slug] = tid
        new_topics.add(str(tid))
        if held:
            held_titles.add(t['title'].lower())
        (held_list if held else created if is_new else updated).append(slug)
        if not held:
            touched.append(str(tid))
    with conn() as c:                                                                  # links, once every topic of this run exists
        for t in analysis['topics']:
            src = by_title.get(t['title'].lower()) or by_title.get(t['slug'])
            for l in t['links']:
                rel = l['relation']
                if rel not in approved_r:                                              # pending/new relation types stay related_to until approved
                    if rel not in known_r and ('relation_type', rel) not in seen_p:
                        proposals.append({'kind': 'relation_type', 'name': rel, 'description': '', 'reason': l['context'] or f"New relation type encountered while processing '{title}'"})
                        seen_p.add(('relation_type', rel))
                    known_r.add(rel)
                    rel = 'related_to'
                dst = by_title.get(l['target'].lower()) or (find(cid, slugify(l['target'])) or find(cid, l['target']) or {}).get('id')
                if src and dst and src != dst:
                    c.execute('INSERT INTO library.topic_link (collection_id,from_topic,to_topic,relation,context) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (from_topic,to_topic,relation) DO UPDATE SET context=EXCLUDED.context, embedding=NULL',
                              (cid, src, dst, rel, l['context']))
        for p in proposals:
            c.execute('INSERT INTO library.vocab_proposal (collection_id,kind,name,description,reason,source_id) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING', (cid, p['kind'], p['name'], p['description'], p['reason'], sid))
            c.execute(f"INSERT INTO library.{p['kind']} (collection_id,name,description,status) VALUES (%s,%s,%s,'pending') ON CONFLICT DO NOTHING", (cid, p['name'], p['description']))
        links = c.execute('SELECT from_topic, to_topic, relation FROM library.topic_link WHERE collection_id=%s AND embedding IS NULL', (cid,)).fetchall()
    for l in links:
        index.embed_link(l['from_topic'], l['to_topic'], l['relation'])
    # --- topics this source used to feed but no longer mentions: supersede its claims there and rewrite surgically now
    revised = []
    for tid in prev_topics - new_topics:
        with conn() as c:
            old = supersede_from_source(c, tid, sid)
            c.execute('DELETE FROM library.topic_source WHERE topic_id=%s AND source_id=%s', (tid, sid))
            left = c.execute('SELECT count(*) AS n FROM library.topic_source WHERE topic_id=%s', (tid,)).fetchone()['n']
            if not left:                                                               # nothing supports it any more: archived (kept, not deleted)
                c.execute("UPDATE library.topic SET status='archived', needs_revision=false, updated_at=now() WHERE id=%s AND status='active'", (tid,))
                continue
        if old:
            try:
                out = surgical_rewrite(cid, tid, [dict(x) for x in old], sid)
            except Exception:
                out = None
            if out:
                store_version(cid, tid, out, f'Revised after source {sid} dropped this topic')
                revised.append(str(tid))
            else:
                with conn() as c:
                    c.execute('UPDATE library.topic SET needs_revision=true WHERE id=%s', (tid,))
            index.embed_topic(cid, tid)
    embed_missing(cid)
    queue_revisions(cid, user)
    link_to_model(cid, analysis['topics'], by_title, held_titles)
    return {'created': created, 'updated': updated, 'held': held_list, 'proposals': len(proposals), 'revised': revised}


# ---------------------------------------------------------------- deciding on held changes
def _claims_of_source(c, tid, sid, status):
    return c.execute("SELECT cl.id, cl.n FROM library.claim cl WHERE cl.topic_id=%s AND cl.status=%s AND EXISTS (SELECT 1 FROM library.claim_source s WHERE s.claim_id=cl.id AND s.source_id=%s)", (tid, status, sid)).fetchall()


def conflict_detail(cid, conflict_id):
    with conn() as c:
        k = c.execute('SELECT * FROM library.topic_conflict WHERE id=%s AND collection_id=%s', (conflict_id, cid)).fetchone()
    return k


def approve(cid, conflict_id, user):
    """Publish the held version: its claims become active (the source's earlier claims on the topic are superseded), a contradicted claim is superseded.
    If the published page moved on meanwhile, or a claim was contradicted, the page is written again on top of the current one from the source wiki."""
    k = conflict_detail(cid, conflict_id)
    if not k or k['status'] != 'open':
        raise LookupError('no such open conflict')
    tid, sid = k['topic_id'], k['source_id']
    with conn() as c:
        held = c.execute('SELECT markdown FROM library.topic_version WHERE topic_id=%s AND version=%s', (tid, k['version'])).fetchone()
        pending = c.execute("SELECT cl.id, cl.n, cl.text, cl.kind, cs.quote FROM library.claim cl JOIN library.claim_source cs ON cs.claim_id=cl.id AND cs.source_id=%s WHERE cl.topic_id=%s AND cl.status='pending'",
                            (sid, tid)).fetchall() if sid else []
        current = _published_version(c, tid)
        markdown, stale = held['markdown'], (current or None) != (k['base_version'] or None)
        if sid:
            c.execute("UPDATE library.claim SET status='superseded', superseded_at=now() WHERE topic_id=%s AND status='active' AND id IN (SELECT claim_id FROM library.claim_source WHERE source_id=%s) AND NOT (id = ANY(%s))",
                      (tid, sid, [p['id'] for p in pending]))
        c.execute("UPDATE library.claim SET status='active' WHERE id = ANY(%s)", ([p['id'] for p in pending],))
        if k['contradicts_n'] is not None:
            c.execute("UPDATE library.claim SET status='superseded', superseded_at=now() WHERE topic_id=%s AND n=%s", (tid, k['contradicts_n']))
        if sid:                                                                         # the source now counts for this topic
            c.execute('INSERT INTO library.topic_source (collection_id,topic_id,source_id,wiki_version) SELECT %s,%s,%s,max(version) FROM library.wiki_version WHERE source_id=%s '
                      'ON CONFLICT (topic_id, source_id) DO UPDATE SET wiki_version=EXCLUDED.wiki_version', (cid, tid, sid, sid))
        row = c.execute('SELECT title, topic_type, summary, slug FROM library.topic WHERE id=%s', (tid,)).fetchone()
    if (stale or k['contradicts_n'] is not None) and current:                          # the held page was written beside facts that have since changed
        _, wiki_md = index.approved_wiki(cid, sid) if sid else (None, None)
        cand = {**row, 'claims': [{'text': p['text'], 'kind': p['kind'], 'quote': p['quote']} for p in pending], 'aliases': [], 'links': []}
        markdown = write_topic_markdown(cid, sid, cand, wiki_md or markdown, current_markdown(tid))
        with conn() as c:
            c.execute("UPDATE library.topic_version SET markdown=%s WHERE topic_id=%s AND version=%s", (markdown, tid, k['version']))
        index.index_topic(cid, tid, k['version'], markdown)
    with conn() as c:
        c.execute("UPDATE library.topic_version SET status='published' WHERE topic_id=%s AND version=%s", (tid, k['version']))
        c.execute("UPDATE library.topic SET status='active', needs_revision=false, updated_at=now() WHERE id=%s", (tid,))
        c.execute("UPDATE library.topic_conflict SET status='approved', decided_by=%s, decided_at=now() WHERE id=%s", (user, conflict_id))
    index.embed_topic(cid, tid)
    embed_missing(cid)


def reject(cid, conflict_id, user):
    """Discard the held change: its version and claims are rejected; a new topic that never had a published page disappears."""
    k = conflict_detail(cid, conflict_id)
    if not k or k['status'] != 'open':
        raise LookupError('no such open conflict')
    with conn() as c:
        tid = k['topic_id']
        c.execute("UPDATE library.topic_version SET status='rejected' WHERE topic_id=%s AND version=%s", (tid, k['version']))
        if k['source_id']:
            c.execute("UPDATE library.claim SET status='rejected' WHERE id = ANY(%s)", ([m['id'] for m in _claims_of_source(c, tid, k['source_id'], 'pending')],))
        if not _published_version(c, tid):
            c.execute('DELETE FROM library.topic WHERE id=%s', (tid,))                  # (its conflict goes with it)
        else:
            c.execute("UPDATE library.topic_conflict SET status='rejected', decided_by=%s, decided_at=now() WHERE id=%s", (user, conflict_id))


def merge(cid, from_id, into_id, user, reason='merged'):
    """Fold a topic into another: its statements (renumbered), aliases, links and sources move over, its address redirects, and the target's page is written again."""
    if str(from_id) == str(into_id):
        raise ValueError('a topic cannot be merged into itself')
    with conn() as c:
        src = c.execute('SELECT * FROM library.topic WHERE id=%s AND collection_id=%s', (from_id, cid)).fetchone()
        dst = c.execute('SELECT * FROM library.topic WHERE id=%s AND collection_id=%s', (into_id, cid)).fetchone()
        if not src or not dst:
            raise LookupError('no such topic')
        known = {norm(r['text']): r['id'] for r in c.execute("SELECT id, text FROM library.claim WHERE topic_id=%s AND status IN ('active','pending')", (into_id,)).fetchall()}
        n = c.execute('SELECT coalesce(max(n),0) AS n FROM library.claim WHERE topic_id=%s', (into_id,)).fetchone()['n']
        for cl in c.execute("SELECT id, text, status FROM library.claim WHERE topic_id=%s AND status IN ('active','pending') ORDER BY n", (from_id,)).fetchall():
            if norm(cl['text']) in known:                                              # the target already states it: its evidence is added to that statement
                c.execute('UPDATE library.claim_source SET claim_id=%s WHERE claim_id=%s', (known[norm(cl['text'])], cl['id']))
            else:
                n += 1
                c.execute("UPDATE library.claim SET topic_id=%s, n=%s, status='active' WHERE id=%s", (into_id, n, cl['id']))
        aliases = [src['title']] + [r['alias'] for r in c.execute('SELECT alias FROM library.topic_alias WHERE topic_id=%s', (from_id,)).fetchall()]
        c.execute('DELETE FROM library.topic_alias WHERE topic_id=%s', (from_id,))
        for a in aliases:
            c.execute('INSERT INTO library.topic_alias (collection_id,topic_id,alias) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING', (cid, into_id, a))
        c.execute('INSERT INTO library.topic_source (collection_id,topic_id,source_id) SELECT collection_id, %s, source_id FROM library.topic_source WHERE topic_id=%s ON CONFLICT DO NOTHING', (into_id, from_id))
        c.execute('INSERT INTO library.topic_link (collection_id,from_topic,to_topic,relation,context) SELECT collection_id, %s, to_topic, relation, context FROM library.topic_link WHERE from_topic=%s AND to_topic<>%s ON CONFLICT DO NOTHING', (into_id, from_id, into_id))
        c.execute('INSERT INTO library.topic_link (collection_id,from_topic,to_topic,relation,context) SELECT collection_id, from_topic, %s, relation, context FROM library.topic_link WHERE to_topic=%s AND from_topic<>%s ON CONFLICT DO NOTHING', (into_id, from_id, into_id))
        c.execute('UPDATE library.topic_redirect SET to_topic=%s WHERE to_topic=%s', (into_id, from_id))
        c.execute('INSERT INTO library.topic_redirect (collection_id,from_slug,to_topic,reason) VALUES (%s,%s,%s,%s) ON CONFLICT (collection_id, from_slug) DO UPDATE SET to_topic=EXCLUDED.to_topic', (cid, src['slug'], into_id, reason))
        merged_md = current_markdown(from_id)
        c.execute('DELETE FROM library.topic WHERE id=%s', (from_id,))                  # versions, links and open conflicts of the merged topic go with it
        old_md = current_markdown(into_id)
    # the merged topic's page is the evidence for amending the target
    md = write_topic_markdown(cid, None, {**dst, 'claims': [], 'aliases': [], 'links': []}, merged_md or src['summary'], old_md)
    store_version(cid, into_id, md, f"merged with '{src['title']}' by {user}")
    with conn() as c:
        c.execute("UPDATE library.topic SET status='active', updated_at=now() WHERE id=%s", (into_id,))
    index.embed_topic(cid, into_id)
    embed_missing(cid)
    return dst['slug']


# ---------------------------------------------------------------- revising pages after a source is gone
def revise(cid, tid):
    """The lint pass: claims superseded since the page was last published are audited by a surgical rewrite against
    the remaining sources. A topic with no source left is archived."""
    with conn() as c:
        t = c.execute('SELECT * FROM library.topic WHERE id=%s AND collection_id=%s', (tid, cid)).fetchone()
        if not t or t['status'] != 'active':
            return 'skipped'
        if not c.execute('SELECT 1 FROM library.topic_source WHERE topic_id=%s', (tid,)).fetchone():
            c.execute("UPDATE library.topic SET status='archived', needs_revision=false, updated_at=now() WHERE id=%s", (tid,))
            return 'archived'
        since = c.execute("SELECT max(created_at) AS at FROM library.topic_version WHERE topic_id=%s AND status='published'", (tid,)).fetchone()['at']
        gone = c.execute("SELECT cl.n, cl.text, (SELECT cs.quote FROM library.claim_source cs WHERE cs.claim_id=cl.id ORDER BY cs.id DESC LIMIT 1) AS quote FROM library.claim cl "
                         "WHERE cl.topic_id=%s AND cl.status='superseded' AND (%s::timestamptz IS NULL OR cl.superseded_at > %s) ORDER BY cl.n", (tid, since, since)).fetchall()
        if not gone:
            c.execute('UPDATE library.topic SET needs_revision=false WHERE id=%s', (tid,))
            return 'unchanged'
    new = surgical_rewrite(cid, tid, [dict(g) for g in gone])
    if not new:
        raise RuntimeError('the writer did not return a page when asked to revise it')
    store_version(cid, tid, new, 'Lint pass: revised after a contributing source was removed or replaced')
    index.embed_topic(cid, tid)
    return 'revised'


def withdraw_source(cid, sid):
    """A source is being deleted: its claims are superseded everywhere (kept, with their quotes, for the lint pass) and the topics that relied on it
    are flagged for a surgical rewrite. Topics left with no source are archived by the lint pass."""
    with conn() as c:
        # a claim another source still supports stays active (one claim is shared between the sources that state it)
        ids = [r['claim_id'] for r in c.execute('SELECT DISTINCT cs.claim_id FROM library.claim_source cs WHERE cs.collection_id=%s AND cs.source_id=%s AND NOT EXISTS '
                                                '(SELECT 1 FROM library.claim_source o WHERE o.claim_id=cs.claim_id AND o.source_id IS NOT NULL AND o.source_id<>%s)', (cid, sid, sid)).fetchall()]
        if ids:
            c.execute("UPDATE library.claim SET status='superseded', superseded_at=now() WHERE id = ANY(%s) AND status IN ('active','pending')", (ids,))
        c.execute('UPDATE library.topic SET needs_revision=true WHERE collection_id=%s AND id IN (SELECT topic_id FROM library.topic_source WHERE source_id=%s)', (cid, sid))
    return len(ids)


def queue_revisions(cid, user=None):
    from . import jobs
    with conn() as c:
        ids = [r['id'] for r in c.execute("SELECT id FROM library.topic WHERE collection_id=%s AND needs_revision AND status='active'", (cid,)).fetchall()]
        for tid in ids:
            if not c.execute("SELECT 1 FROM library.job WHERE kind='topic.revise' AND collection_id=%s AND payload->>'topic_id'=%s AND status IN ('queued','running')", (cid, str(tid))).fetchone():
                c.execute("INSERT INTO library.job (kind,collection_id,payload,created_by) VALUES ('topic.revise',%s,%s,%s)", (cid, J({'topic_id': str(tid)}), user))
    return len(ids)


def queue(cid, sid, user=None):
    """Ask for the topic wiki to be updated from a source, unless that is already waiting."""
    from . import jobs
    with conn() as c:
        if c.execute("SELECT 1 FROM library.job WHERE kind='source.topics' AND source_id=%s AND status IN ('queued','running')", (sid,)).fetchone():
            return None
    return jobs.enqueue('source.topics', cid, sid, user=user)


def embed_missing(cid=None, limit=500):
    """Compute the vectors that are missing (new ones, or after the embedding size changed): active claims, topic identities (with their claims),
    then wiki chunks, topic chunks and links. Returns how many."""
    if not embeddings.available():
        return 0
    cast = vectors.spec(config.EMBEDDING_DIM)['cast']
    done = 0
    with conn() as c:
        claims = c.execute("SELECT cl.id, cl.text FROM library.claim cl JOIN library.topic t ON t.id=cl.topic_id WHERE cl.embedding IS NULL AND cl.status='active' AND t.status='active' AND (%s::uuid IS NULL OR cl.collection_id=%s) ORDER BY cl.id LIMIT %s", (cid, cid, limit)).fetchall()
        tops = c.execute("SELECT id, collection_id FROM library.topic WHERE embedding IS NULL AND status='active' AND (%s::uuid IS NULL OR collection_id=%s) ORDER BY id LIMIT %s", (cid, cid, limit)).fetchall()
    try:
        for i in range(0, len(claims), 16):
            batch = claims[i:i + 16]
            vecs = embeddings.embed([b['text'] for b in batch])
            with conn() as c:
                for b, v in zip(batch, vecs):
                    c.execute(f'UPDATE library.claim SET embedding=%s::{cast} WHERE id=%s', (embeddings.vec_literal(v), b['id']))
            done += len(batch)
    except RuntimeError:
        return done
    for t in tops:
        index.embed_topic(t['collection_id'], t['id'])
        done += 1
    return done + index.backfill(limit)


def link_to_model(cid, topics_, by_title, held_titles):
    """The analyzer saw the data model while it read the source: record the entities it says each topic is about. A topic still held for review gets its links when it is approved."""
    from . import bridge
    for t in topics_:
        tid = by_title.get(t['title'].lower())
        if not tid or t['title'].lower() in held_titles:
            continue
        for m in t.get('model_links') or []:
            try:
                conf = max(0.0, min(1.0, float(m.get('confidence', 0))))
                if m.get('relation') in bridge.RELATIONS:
                    bridge.set_link(cid, tid, 'entity', str(m.get('entity', '')), m['relation'], 'ai', 'confirmed' if conf >= bridge.AUTO_CONFIRM else 'suggested', 'ai', conf, str(m.get('reason') or ''))
            except (ValueError, TypeError):
                continue                                                              # an entity that is not in the model: dropped


def queue_model_links(cid, topic_ids, user=None):
    """Ask for the topics to be connected to model entities (a background job; skipped when there is no published model)."""
    from . import bridge, jobs
    if topic_ids and bridge._onto():
        jobs.enqueue('topic.link_model', cid, None, {'topic_ids': list(topic_ids)}, user)
