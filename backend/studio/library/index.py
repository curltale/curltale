"""The searchable layers of a collection (kept collection by collection):

    library.source_chunk   the original text of a source            (written by pipeline.ingest)
    library.wiki_chunk     every version of every source wiki        index_wiki
    library.topic_chunk    every version of every topic page         index_topic
    library.topic.embedding   a topic's identity: title, aliases, summary and its active claims   embed_topic
    library.claim.embedding   each claim                              (topics.embed_missing)
    library.topic_link.embedding   each typed relationship with its context   embed_link

Every search here is hybrid: meaning (vectors) and words (full text) combined by reciprocal rank, so an exact code or number is found even when its
vector is not close, and a paraphrase is found even when no word matches. Every function takes a collection id and filters on it: nothing searches
across collections. Only what is served is searched: the approved version of a source wiki and the published version of an active topic."""
import re
from .. import config, embeddings, settings, vectors
from ..db import conn
from . import chunking

WIKI_CHUNK_TOKENS = 600                       # never more than the embedding model reads whole
BATCH = 16


def norm(text):
    return re.sub(r'\s+', ' ', (text or '').strip().lower())


def chunk_markdown(markdown):
    return chunking.chunk(markdown or '', target=min(WIKI_CHUNK_TOKENS, chunking.target_tokens()), overlap=0)


def _embed_rows(table, rows):
    """rows: [(id, text)] -> fills the vectors it can; a missing or failing embedding model leaves them for the backfill."""
    if not rows or not embeddings.available():
        return 0
    cast = vectors.spec(config.EMBEDDING_DIM)['cast']
    done = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        try:
            vecs = embeddings.embed([t for _, t in batch])
        except RuntimeError:
            return done
        with conn() as c:
            for (rid, _), v in zip(batch, vecs):
                c.execute(f'UPDATE {table} SET embedding=%s::{cast} WHERE id=%s', (embeddings.vec_literal(v), rid))
        done += len(batch)
    return done


def _store_chunks(table, owner, cid, owner_id, version, markdown):
    parts = chunk_markdown(markdown)
    ids = []
    with conn() as c:
        c.execute(f'DELETE FROM {table} WHERE {owner}=%s AND version=%s', (owner_id, version))
        for p in parts:
            ids.append((c.execute(f'INSERT INTO {table} (collection_id,{owner},version,idx,text,tokens,heading_path) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                                  (cid, owner_id, version, p['idx'], p['text'], p['tokens'], p['heading_path'])).fetchone()['id'], p['text']))
    _embed_rows(table, ids)
    return len(parts)


def index_wiki(cid, sid, version, markdown):
    """Chunk and embed one version of a source wiki (every version is kept and indexed)."""
    return _store_chunks('library.wiki_chunk', 'source_id', cid, sid, version, markdown)


def index_topic(cid, tid, version, markdown):
    """Chunk and embed one version of a topic page (held drafts too: they are only searched once published)."""
    return _store_chunks('library.topic_chunk', 'topic_id', cid, tid, version, markdown)


def embed_topic(cid, tid):
    """A topic's identity vector, from its title, aliases, summary and active claims, rebuilt after every write so that search hits on what the topic knows, not only on its name."""
    with conn() as c:
        t = c.execute('SELECT title, summary FROM library.topic WHERE id=%s AND collection_id=%s', (tid, cid)).fetchone()
        if not t:
            return
        aliases = [r['alias'] for r in c.execute('SELECT alias FROM library.topic_alias WHERE topic_id=%s ORDER BY alias', (tid,)).fetchall()]
        claims = [r['text'] for r in c.execute("SELECT text FROM library.claim WHERE topic_id=%s AND status='active' ORDER BY n", (tid,)).fetchall()]
    text = f"{t['title']}\n\n{', '.join(a for a in aliases if a != t['title'])}\n\n{t['summary']}\n\n" + '\n'.join(claims)
    text = ' '.join(text.split()[:3000])                                                       # about 3,000 words
    if not embeddings.available():
        return
    try:
        v = embeddings.embed([text])[0]
    except RuntimeError:
        return
    cast = vectors.spec(config.EMBEDDING_DIM)['cast']
    with conn() as c:
        c.execute(f'UPDATE library.topic SET embedding=%s::{cast} WHERE id=%s', (embeddings.vec_literal(v), tid))


def _link_text(c, from_topic, to_topic, relation, context):
    names = {r['id']: r['title'] for r in c.execute('SELECT id, title FROM library.topic WHERE id = ANY(%s)', ([from_topic, to_topic],)).fetchall()}
    return f"{names.get(from_topic, '')} {relation.replace('_', ' ')} {names.get(to_topic, '')}. {context or ''}".strip()


def embed_link(from_topic, to_topic, relation):
    with conn() as c:
        r = c.execute('SELECT context FROM library.topic_link WHERE from_topic=%s AND to_topic=%s AND relation=%s', (from_topic, to_topic, relation)).fetchone()
        if not r or not embeddings.available():
            return
        text = _link_text(c, from_topic, to_topic, relation, r['context'])
    try:
        v = embeddings.embed([text])[0]
    except RuntimeError:
        return
    cast = vectors.spec(config.EMBEDDING_DIM)['cast']
    with conn() as c:
        c.execute(f'UPDATE library.topic_link SET embedding=%s::{cast} WHERE from_topic=%s AND to_topic=%s AND relation=%s', (embeddings.vec_literal(v), from_topic, to_topic, relation))


def backfill(limit=2000):
    """Vectors still missing (no embedding model at the time, or a changed vector size): wiki chunks, topic chunks, then links. -> how many."""
    if not embeddings.available():
        return 0
    n = 0
    for table in ('library.wiki_chunk', 'library.topic_chunk'):
        with conn() as c:
            rows = c.execute(f'SELECT id, text FROM {table} WHERE embedding IS NULL ORDER BY id LIMIT %s', (limit,)).fetchall()
        n += _embed_rows(table, [(r['id'], r['text']) for r in rows])
    with conn() as c:
        links = c.execute('SELECT from_topic, to_topic, relation FROM library.topic_link WHERE embedding IS NULL LIMIT %s', (limit,)).fetchall()
    for l in links:
        embed_link(l['from_topic'], l['to_topic'], l['relation'])
        n += 1
    return n


def quote_place(cid, sid, version, quote):
    """The chunk of a wiki version that holds a quote word for word (whitespace and case aside), or None."""
    q = norm(quote)
    if not q:
        return None
    with conn() as c:
        for r in c.execute('SELECT idx, text FROM library.wiki_chunk WHERE collection_id=%s AND source_id=%s AND version=%s ORDER BY idx', (cid, sid, version)).fetchall():
            if q in norm(r['text']):
                return r['idx']
    return None


# ---------------------------------------------------------------- hybrid search
def _hybrid(c, cols, from_sql, where_sql, params, query, pool):
    """Run the same selection by meaning and by words; -> (rows by id, scores by id). The chunk table must be aliased `x` in from_sql."""
    score, rows = {}, {}
    lists = []
    if embeddings.available():
        try:
            q = embeddings.vec_literal(embeddings.embed_query(query))
            cast = vectors.spec(config.EMBEDDING_DIM)['cast']
            c.execute('SET LOCAL hnsw.iterative_scan = strict_order')
            floor = settings.get('LIBRARY_MIN_SIMILARITY')                                       # below it, a nearest neighbour is not an answer
            lists.append([r for r in c.execute(f'SELECT {cols}, 1 - (x.embedding <=> %s::{cast}) AS sim FROM {from_sql} {where_sql} AND x.embedding IS NOT NULL ORDER BY x.embedding <=> %s::{cast} LIMIT %s',
                                               (q, *params, q, pool)).fetchall() if r['sim'] >= floor])
        except RuntimeError:
            pass
    words = ' or '.join(re.findall(r'\w{2,}', query.lower())[:16])
    if words:
        lists.append(c.execute(f"SELECT {cols}, NULL::float AS sim FROM {from_sql} {where_sql} AND x.tsv @@ websearch_to_tsquery('simple', %s) ORDER BY ts_rank(x.tsv, websearch_to_tsquery('simple', %s)) DESC LIMIT %s",
                               (*params, words, words, pool)).fetchall())
    for lst in lists:
        for rank, r in enumerate(lst):
            score[r['id']] = score.get(r['id'], 0) + 1 / (60 + rank)
            if r['id'] not in rows or (r.get('sim') is not None and rows[r['id']].get('sim') is None):
                rows[r['id']] = r
    return rows, score


def search_topic_chunks(cid, query, pool=50):
    """Chunks of the published version of active topics of one collection, best first: [{topic_id, slug, title, topic_type, summary, idx, text, tokens, score, sim}]."""
    query = (query or '').strip()
    if not query:
        return []
    cols = 'x.id, x.topic_id, x.idx, x.text, x.tokens, x.heading_path, t.slug, t.title, t.topic_type, t.summary'
    frm = 'library.topic_chunk x JOIN library.topic t ON t.id=x.topic_id AND t.collection_id=x.collection_id'
    where = ("WHERE x.collection_id=%s AND t.status='active' AND x.version = (SELECT max(v.version) FROM library.topic_version v WHERE v.topic_id=x.topic_id AND v.status='published')")
    with conn() as c:
        rows, score = _hybrid(c, cols, frm, where, (cid,), query, pool)
    return [{**rows[i], 'score': score[i]} for i in sorted(score, key=score.get, reverse=True)]


def search_wiki_chunks(cid, query, pool=40, exclude_source=None):
    """Chunks of the approved (current) wiki of each source of one collection, best first."""
    query = (query or '').strip()
    if not query:
        return []
    cols = 'x.id, x.source_id, x.version, x.idx, x.text, x.heading_path, s.name, s.title, s.url'
    frm = 'library.wiki_chunk x JOIN library.source s ON s.id=x.source_id AND s.collection_id=x.collection_id'
    where = ("WHERE x.collection_id=%s AND s.wiki_status='approved' AND x.version = (SELECT max(w.version) FROM library.wiki_version w WHERE w.source_id=x.source_id)"
             + (' AND x.source_id <> %s' if exclude_source else ''))
    with conn() as c:
        rows, score = _hybrid(c, cols, frm, where, (cid, *([exclude_source] if exclude_source else [])), query, pool)
    return [{**rows[i], 'score': score[i]} for i in sorted(score, key=score.get, reverse=True)]


def search_links(cid, query, pool=20):
    """Typed relationships of one collection whose meaning or words match: [{from_topic, to_topic, relation, context, score}] (active topics only)."""
    query = (query or '').strip()
    if not query:
        return []
    out, score = {}, {}
    with conn() as c:
        lists = []
        if embeddings.available():
            try:
                q = embeddings.vec_literal(embeddings.embed_query(query))
                cast = vectors.spec(config.EMBEDDING_DIM)['cast']
                lists.append(c.execute(f"SELECT l.from_topic, l.to_topic, l.relation, l.context FROM library.topic_link l JOIN library.topic a ON a.id=l.from_topic AND a.status='active' "
                                       f"JOIN library.topic b ON b.id=l.to_topic AND b.status='active' WHERE l.collection_id=%s AND l.embedding IS NOT NULL ORDER BY l.embedding <=> %s::{cast} LIMIT %s",
                                       (cid, q, pool)).fetchall())
            except RuntimeError:
                pass
        words = ' or '.join(re.findall(r'\w{2,}', query.lower())[:16])
        if words:
            lists.append(c.execute("SELECT l.from_topic, l.to_topic, l.relation, l.context FROM library.topic_link l JOIN library.topic a ON a.id=l.from_topic AND a.status='active' "
                                   "JOIN library.topic b ON b.id=l.to_topic AND b.status='active' WHERE l.collection_id=%s AND to_tsvector('simple', l.context || ' ' || replace(l.relation,'_',' ')) @@ websearch_to_tsquery('simple', %s) LIMIT %s",
                                   (cid, words, pool)).fetchall())
    for lst in lists:
        for rank, r in enumerate(lst):
            k = (r['from_topic'], r['to_topic'], r['relation'])
            score[k] = score.get(k, 0) + 1 / (60 + rank)
            out[k] = r
    return [{**out[k], 'score': score[k]} for k in sorted(score, key=score.get, reverse=True)]


def published(cid, tid):
    """(version, markdown) of a topic's published page, or (None, '')."""
    with conn() as c:
        r = c.execute("SELECT version, markdown FROM library.topic_version WHERE topic_id=%s AND collection_id=%s AND status='published' ORDER BY version DESC LIMIT 1", (tid, cid)).fetchone()
    return (r['version'], r['markdown']) if r else (None, '')


def topic_window(cid, tid, version, idx, before=1, after=1):
    with conn() as c:
        return c.execute('SELECT idx, text, tokens FROM library.topic_chunk WHERE collection_id=%s AND topic_id=%s AND version=%s AND idx BETWEEN %s AND %s ORDER BY idx',
                         (cid, tid, version, max(0, idx - before), idx + after)).fetchall()


def approved_wiki(cid, sid):
    """(version, markdown) of a source's approved wiki, or (None, None)."""
    with conn() as c:
        r = c.execute("SELECT w.version, w.markdown FROM library.wiki_version w JOIN library.source s ON s.id=w.source_id AND s.collection_id=w.collection_id "
                      "WHERE w.source_id=%s AND w.collection_id=%s AND s.wiki_status='approved' ORDER BY w.version DESC LIMIT 1", (sid, cid)).fetchone()
    return (r['version'], r['markdown']) if r else (None, None)
