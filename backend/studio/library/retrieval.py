"""Finding passages. Every function takes a Collection handle (from scope.open_collection), never a bare id, and puts collection_id in its WHERE:
there is no way to search across collections from here."""
from .. import config, embeddings, settings, vectors
from ..db import conn

K_MAX = 30


def _rank(rows):
    return {r['id']: i for i, r in enumerate(rows)}


def search(col, query, k=8, source_id=None):
    """Passages of this collection for a question: meaning (vectors) and words (keywords) combined by reciprocal rank. -> list of dicts."""
    k = max(1, min(int(k), K_MAX))
    query = (query or '').strip()
    if not query:
        return []
    only = ' AND c.source_id=%s' if source_id else ''
    extra = (str(source_id),) if source_id else ()
    cols = 'c.id, c.source_id, s.name AS source, c.idx, c.text, c.location, c.heading_path'
    pool = k * 4
    with conn() as c:
        by_meaning, by_words = [], []
        if embeddings.available() and c.execute(f'SELECT 1 FROM library.source_chunk c WHERE c.collection_id=%s AND c.embedding IS NOT NULL{only} LIMIT 1', (col.id, *extra)).fetchone():
            try:
                q = embeddings.vec_literal(embeddings.embed_query(query))
                cast = vectors.spec(config.EMBEDDING_DIM)['cast']
                c.execute('SET LOCAL hnsw.iterative_scan = strict_order')       # a filter on the collection must not starve the index scan
                by_meaning = c.execute(f'SELECT {cols}, 1 - (c.embedding <=> %s::{cast}) AS sim FROM library.source_chunk c JOIN library.source s ON s.id=c.source_id AND s.collection_id=c.collection_id '
                                       f'WHERE c.collection_id=%s AND c.embedding IS NOT NULL{only} ORDER BY c.embedding <=> %s::{cast} LIMIT %s', (q, col.id, *extra, q, pool)).fetchall()
                floor = settings.get('LIBRARY_MIN_SIMILARITY')
                by_meaning = [r for r in by_meaning if r['sim'] >= floor]
            except RuntimeError:                                                # the embedding service is down: keywords still work
                by_meaning = []
        by_words = c.execute(f"SELECT {cols} FROM library.source_chunk c JOIN library.source s ON s.id=c.source_id AND s.collection_id=c.collection_id "
                             f"WHERE c.collection_id=%s AND c.tsv @@ websearch_to_tsquery('simple', %s){only} ORDER BY ts_rank(c.tsv, websearch_to_tsquery('simple', %s)) DESC LIMIT %s",
                             (col.id, query, *extra, query, pool)).fetchall()
    score, byid = {}, {}
    for lst in (by_meaning, by_words):
        for rank, r in enumerate(lst):
            score[r['id']] = score.get(r['id'], 0) + 1 / (60 + rank)
            byid[r['id']] = r
    top = sorted(score, key=score.get, reverse=True)[:k]
    return [{'passage': i, 'source_id': str(byid[i]['source_id']), 'source': byid[i]['source'], 'chunk_index': byid[i]['idx'], 'location': byid[i]['location'], 'section': ' > '.join(byid[i]['heading_path'] or []) or None,
             'text': byid[i]['text'], 'score': round(score[i], 4), 'similarity': round(byid[i]['sim'], 3) if byid[i].get('sim') is not None else None, 'found_by': [n for n, l in (('meaning', by_meaning), ('words', by_words)) if i in _rank(l)]} for i in top]
