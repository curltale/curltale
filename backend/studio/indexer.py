"""Derive search chunks (and their embeddings) for a version."""
import hashlib
import json
from . import config, embeddings
from .chunker import build_chunks, summarize
from .db import conn, J
from .ontology import assemble

BATCH = 64


def chunk_hash(c):
    return hashlib.sha256((c['title'] + '\n' + c['text']).encode()).hexdigest()


def reindex(vid, embed=True, progress=None):
    """Rebuild all chunks of a version. Embeddings are reused by content hash from any version;
    only new or changed text is sent to the embedding model."""
    chunks = build_chunks(assemble(vid))
    for c in chunks:
        c['hash'] = chunk_hash(c)
    with conn() as c:
        known = {r['content_hash']: r['emb'] for r in c.execute(
            'SELECT content_hash, embedding::text AS emb FROM embedding_cache WHERE model=%s AND content_hash = ANY(%s)', (config.EMBEDDING_MODEL, [ch['hash'] for ch in chunks]))}
        known.update({r['content_hash']: r['emb'] for r in c.execute(
            'SELECT DISTINCT ON (content_hash) content_hash, embedding::text AS emb FROM chunk WHERE embedding IS NOT NULL')})
        c.execute('DELETE FROM chunk WHERE version_id=%s', (vid,))
        rows = []
        for ch in chunks:
            m = ch['metadata']
            rows.append((vid, ch['id'], ch['type'], ch['title'], ch['text'], m.get('entity'), m.get('agent_access'),
                         m.get('sensitivity'), m.get('review_status'), J(m), ch['hash'], known.get(ch['hash'])))
        with c.cursor() as cur:
            cur.executemany('INSERT INTO chunk (version_id,id,type,title,text,entity,agent_access,sensitivity,review_status,metadata,content_hash,embedding) '
                            'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::vector)',
                            [(*r[:9], json.dumps(r[9].obj), r[10], r[11]) for r in rows])
    reused = sum(1 for ch in chunks if ch['hash'] in known)
    embedded = backfill(vid, progress) if embed and embeddings.available() else 0
    remember(vid)
    return {'chunks': len(chunks), 'reused_embeddings': reused, 'newly_embedded': embedded, 'by_type': dict(summarize(chunks)),
            'missing_embeddings': missing(vid)}


def missing(vid):
    with conn() as c:
        return c.execute('SELECT count(*) AS n FROM chunk WHERE version_id=%s AND embedding IS NULL', (vid,)).fetchone()['n']


def remember(vid):
    """Keep this version's embeddings in the cache (by content), so they outlive the version."""
    with conn() as c:
        c.execute('INSERT INTO embedding_cache (content_hash, model, embedding) SELECT DISTINCT ON (content_hash) content_hash, %s, embedding FROM chunk '
                  'WHERE version_id=%s AND embedding IS NOT NULL ON CONFLICT DO NOTHING', (config.EMBEDDING_MODEL, vid))


def backfill(vid, progress=None):
    """Embed every chunk of the version that has no embedding yet. Returns how many were embedded. progress(done, total) is called after every batch."""
    done = 0
    total = missing(vid)
    if progress:
        progress(0, total)
    while True:
        with conn() as c:
            todo = c.execute('SELECT id, title, text, content_hash FROM chunk WHERE version_id=%s AND embedding IS NULL LIMIT %s', (vid, BATCH)).fetchall()
        if not todo:
            return done
        vecs = embeddings.embed([t['title'] + '\n' + t['text'] for t in todo])
        with conn() as c:
            with c.cursor() as cur:
                # same text in other chunks of this version gets the same vector
                cur.executemany('UPDATE chunk SET embedding=%s::vector WHERE version_id=%s AND content_hash=%s AND embedding IS NULL',
                                [(embeddings.vec_literal(v), vid, t['content_hash']) for t, v in zip(todo, vecs)])
        remember(vid)                                          # a publish that is interrupted keeps what it has embedded so far
        done += len(todo)
        if progress:
            progress(min(done, total), total)
