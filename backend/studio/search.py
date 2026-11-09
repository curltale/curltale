"""Hybrid search over a version's chunks: vector + keyword + exact code match, merged with reciprocal rank fusion."""
import re
from . import access, embeddings
from .db import conn

STOP = set('a an and are as at be by can do does for from get how i if in is it of on or our so that the their there this to us was we what when where which who why will with you your me my show tell give find list all any each per about between into over than then them these those'.split())
RANK_K = 60
PENDING = ('needs_review', 'source_needed')

SQL_VEC = """SELECT id, row_number() OVER (ORDER BY embedding <=> %(qv)s::vector) AS r FROM chunk
             WHERE version_id=%(v)s AND embedding IS NOT NULL AND (agent_access IS DISTINCT FROM 'none' OR %(see_none)s) AND coalesce(sensitivity,'internal') <> ALL(%(denied)s)
               AND (%(types)s::text[] IS NULL OR type = ANY(%(types)s)) ORDER BY embedding <=> %(qv)s::vector LIMIT 50"""
SQL_KW = """SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) AS r FROM chunk, to_tsquery('simple', %(tsq)s) q
            WHERE version_id=%(v)s AND tsv @@ q AND (agent_access IS DISTINCT FROM 'none' OR %(see_none)s) AND coalesce(sensitivity,'internal') <> ALL(%(denied)s)
              AND (%(types)s::text[] IS NULL OR type = ANY(%(types)s)) ORDER BY ts_rank_cd(tsv, q) DESC LIMIT 50"""
# internal codes (column names, abbreviations, record ids) must hit exactly: shortest chunks first
SQL_EXACT = """SELECT id, row_number() OVER (ORDER BY (lower(title) = ANY(%(lc)s) OR lower(id) LIKE ANY(%(idend)s)) DESC, length(text)) AS r FROM chunk
               WHERE version_id=%(v)s AND (agent_access IS DISTINCT FROM 'none' OR %(see_none)s) AND coalesce(sensitivity,'internal') <> ALL(%(denied)s) AND (%(types)s::text[] IS NULL OR type = ANY(%(types)s))
                 AND (lower(title) = ANY(%(lc)s) OR lower(id) LIKE ANY(%(idlike)s) OR text ~* ANY(%(rx)s))
               ORDER BY (lower(title) = ANY(%(lc)s) OR lower(id) LIKE ANY(%(idend)s)) DESC, length(text) LIMIT 30"""


def _terms(q):
    toks = re.findall(r'[A-Za-z0-9_]+', q)
    words = [t.lower() for t in toks if t.lower() not in STOP]
    parts = []
    for w in words:
        parts += [p for p in w.split('_') if p and p not in STOP]
    codes = [t for t in toks if ('_' in t or (any(ch.isdigit() for ch in t) and len(t) > 2) or (t.isupper() and len(t) >= 2 and t.lower() not in STOP))]
    return parts, codes


def search(vid, q, types=None, k=12, expand=True, use_vectors=True, ent=None):
    denied, see_none = access.sql_filter(ent)
    parts, codes = _terms(q)
    types = types or None
    ranks = []
    with conn() as c:
        if use_vectors and embeddings.available():
            try:
                qv = embeddings.vec_literal(embeddings.embed_query(q))
                ranks.append(c.execute(SQL_VEC, {'qv': qv, 'v': vid, 'types': types, 'denied': denied, 'see_none': see_none}).fetchall())
            except RuntimeError:
                pass
        if parts:
            tsq = ' | '.join(sorted(set(parts)))
            ranks.append(c.execute(SQL_KW, {'tsq': tsq, 'v': vid, 'types': types, 'denied': denied, 'see_none': see_none}).fetchall())
        if codes:
            lc = [x.lower() for x in codes]
            ranks.append(c.execute(SQL_EXACT, {'v': vid, 'types': types, 'denied': denied, 'see_none': see_none, 'lc': lc,
                                               'idlike': [f'%{x}%' for x in lc], 'idend': [f'%.{x}' for x in lc] + [f'%:{x}' for x in lc], 'rx': [r'\m' + re.escape(x) + r'\M' for x in codes]}).fetchall())
        score = {}
        for lst in ranks:
            for r in lst:
                score[r['id']] = score.get(r['id'], 0) + 1.0 / (RANK_K + r['r'])
        top = sorted(score, key=score.get, reverse=True)[:k]
        extra = []
        if expand and top:
            ents = c.execute("SELECT DISTINCT entity FROM chunk WHERE version_id=%s AND id = ANY(%s) AND type IN ('attribute','calculated','check','filter') AND entity IS NOT NULL",
                             (vid, top)).fetchall()
            extra = [f"entity:{e['entity']}" for e in ents if f"entity:{e['entity']}" not in top]
        rows = c.execute('SELECT id,type,title,text,entity,review_status,metadata FROM chunk WHERE version_id=%s AND id = ANY(%s)', (vid, top + extra)).fetchall()
    by_id = {r['id']: r for r in rows}
    out = []
    for cid in top + extra:
        r = by_id.get(cid)
        if r:
            out.append({'id': cid, 'type': r['type'], 'title': r['title'], 'text': r['text'], 'entity': r['entity'],
                        'score': round(score.get(cid, 0), 5), 'expanded': cid in extra, 'kb_type': r['metadata'].get('kb_type'),
                        'pending_confirmation': r['review_status'] in PENDING})
    return out
