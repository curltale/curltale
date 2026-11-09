"""The bridge between the Library and the governed model. Links go from a topic to model entities and fields and are stored in the collection; they are
resolved against the published model whenever read, using the reader's own entitlements (a field the reader may not see is not shown), and they are only
ever visible through collections the reader can read."""
import json
from .. import access, llm, tools
from .. import search as model_search
from ..db import conn
from . import scope as S, wiki

RELATIONS = ('instance_of', 'about', 'governs', 'uses')
AUTO_CONFIRM = 0.7                       # an AI link at least this sure counts at once; below it waits for a person
LABEL = {'instance_of': 'is a kind of', 'about': 'is about', 'governs': 'governs', 'uses': 'concerns'}


def _onto():
    try:
        return tools._onto(tools._vid())
    except RuntimeError:                  # no published model yet
        return None


def resolve(refs, ent=None):
    """Rows of topic_model_ref -> what to show, for a reader with these entitlements. Unknown or withheld fields are left out; an entity that has left the model is flagged."""
    onto = _onto()
    ent = ent or access.caller()[1]
    out = []
    for r in refs:
        if r['kind'] == 'entity':
            e = (onto or {}).get('entities', {}).get(r['ref'])
            out.append({'kind': 'entity', 'ref': r['ref'], 'name': e['name'] if e else r['ref'], 'in_model': bool(e) if onto else None, 'relation': r['relation'], 'label': LABEL[r['relation']],
                        'status': r['status'], 'origin': r['origin'], 'confidence': r.get('confidence'), 'reason': r.get('reason', '')})
        else:
            ek, _, col = r['ref'].partition('.')
            a = ((onto or {}).get('entities', {}).get(ek) or {}).get('attributes', {}).get(col)
            if a is not None and not access.can_see(a.get('sensitivity'), a.get('agent_access'), ent):
                continue                                                          # withheld from this reader, and not confirmed to exist
            out.append({'kind': 'field', 'ref': r['ref'], 'name': a['business_name'] if a else r['ref'], 'in_model': (a is not None) if onto else None, 'relation': r['relation'], 'label': LABEL[r['relation']],
                        'status': r['status'], 'origin': r['origin'], 'confidence': r.get('confidence'), 'reason': r.get('reason', '')})
    return out


def refs_of(tid, cid, include_rejected=False):
    with conn() as c:
        return c.execute("SELECT kind, ref, relation, status, origin, confidence, reason FROM library.topic_model_ref WHERE topic_id=%s AND collection_id=%s AND (%s OR status<>'rejected') ORDER BY kind, ref", (tid, cid, include_rejected)).fetchall()


def check_ref(kind, ref):
    """Does this entity or field exist in the published model? -> the canonical ref, or raises ValueError. (Existence of a field is checked only against the structure that anyone may see.)"""
    onto = _onto()
    if onto is None:
        raise ValueError('there is no published model to link to yet')
    if kind == 'entity':
        key = tools._find_entity(onto, ref)
        if not key:
            raise ValueError(f'no entity {ref!r} in the model')
        return key
    ek, _, col = ref.partition('.')
    key = tools._find_entity(onto, ek)
    if not key or col not in onto['entities'][key]['attributes']:
        raise ValueError(f'no field {ref!r} in the model')
    a = onto['entities'][key]['attributes'][col]
    if not access.can_see(a.get('sensitivity'), a.get('agent_access'), {k: False for k in access.LEVELS} | {'data': False}):      # the weakest reader decides: links never point at personal, confidential or restricted fields
        raise ValueError(f'no field {ref!r} in the model')
    return f'{key}.{col}'


def set_link(cid, tid, kind, ref, relation, user, status='confirmed', origin='manual', confidence=None, reason=''):
    if relation not in RELATIONS:
        raise ValueError(f"relation must be one of {', '.join(RELATIONS)}")
    ref = check_ref(kind, ref)
    with conn() as c:
        c.execute("INSERT INTO library.topic_model_ref (collection_id,topic_id,kind,ref,relation,status,origin,confidence,reason,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                  "ON CONFLICT (topic_id, kind, ref) DO UPDATE SET relation=EXCLUDED.relation, status=EXCLUDED.status, origin=EXCLUDED.origin, created_by=EXCLUDED.created_by, reason=EXCLUDED.reason "
                  "WHERE %s = 'manual' OR library.topic_model_ref.status <> 'rejected'", (cid, tid, kind, ref, relation, status, origin, confidence, reason[:300], user, origin))
    return ref


def decide(cid, tid, kind, ref, accept, user):
    with conn() as c:
        r = c.execute("UPDATE library.topic_model_ref SET status=%s, created_by=%s WHERE topic_id=%s AND collection_id=%s AND kind=%s AND ref=%s RETURNING ref",
                      ('confirmed' if accept else 'rejected', user, tid, cid, kind, ref)).fetchone()
    if not r:
        raise LookupError('no such link')


def remove(cid, tid, kind, ref):
    """A person's own link is deleted; an AI link is marked rejected so that it is not proposed again."""
    with conn() as c:
        c.execute("DELETE FROM library.topic_model_ref WHERE topic_id=%s AND collection_id=%s AND kind=%s AND ref=%s AND origin='manual'", (tid, cid, kind, ref))
        c.execute("UPDATE library.topic_model_ref SET status='rejected' WHERE topic_id=%s AND collection_id=%s AND kind=%s AND ref=%s AND origin='ai'", (tid, cid, kind, ref))


MAX_LINKS = 12                  # a safety net, not a target: how many links one topic may get in one pass
WHOLE_MODEL = 200               # a model up to this many entities is shown to the linker whole; a bigger one is narrowed to the likely candidates first


def entity_catalog(topic_text=None):
    """The entities an automatic linker may be shown: names and definitions only (structure anyone may see), nothing about data.
    A very large model is narrowed to the entities that look like the topic (by meaning and by name), so none is missed for being far down a list."""
    onto = _onto()
    if not onto:
        return {}
    ents = onto['entities']
    if len(ents) <= WHOLE_MODEL or not topic_text:
        return dict(list(ents.items())[:WHOLE_MODEL]) if len(ents) > WHOLE_MODEL and not topic_text else ents
    access.set_caller('library-linker', None)                                  # the weakest reader: structure only
    keys = []
    for r in model_search.search(tools._vid(), topic_text, types=['entity'], k=60):
        k = str(r.get('id', '')).split(':', 1)[-1]
        if k in ents and k not in keys:
            keys.append(k)
    return {k: ents[k] for k in keys} or dict(list(ents.items())[:WHOLE_MODEL])


def suggest(cid, tid, user='system'):
    """Ask the model-linker for entities this topic is about; save the answers (never over a person's decision). Returns how many links were saved."""
    with conn() as c:
        t = c.execute('SELECT title, topic_type, summary FROM library.topic WHERE id=%s AND collection_id=%s', (tid, cid)).fetchone()
        claims = c.execute("SELECT text FROM library.claim WHERE topic_id=%s AND status='active' ORDER BY n LIMIT 6", (tid,)).fetchall()
    if not t:
        return 0
    cat = entity_catalog(f"{t['title']}. {t['summary']}")
    if not cat:
        return 0
    listing = '\n'.join(f"{k} | {e['name']} | {e.get('domain') or ''} | {(e.get('definition') or '')[:220]}" for k, e in cat.items())
    user_msg = (f"TOPIC\nTitle: {t['title']}\nType: {t['topic_type']}\nSummary: {t['summary']}\nStatements:\n" + '\n'.join('- ' + x['text'] for x in claims)
                + f'\n\nENTITIES (key | name | domain | definition)\n{listing}')
    text = llm.chat([{'role': 'system', 'content': (wiki.PROMPTS / 'model_linker.md').read_text()}, {'role': 'user', 'content': user_msg}], role='linker', max_tokens=800, temperature=0, timeout=90)
    data = wiki._json_object(text) or {}
    saved = 0
    for l in (data.get('links') or [])[:MAX_LINKS]:
        if not isinstance(l, dict) or l.get('entity') not in cat or l.get('relation') not in RELATIONS:
            continue
        try:
            conf = max(0.0, min(1.0, float(l.get('confidence', 0))))
        except (TypeError, ValueError):
            continue
        set_link(cid, tid, 'entity', l['entity'], l['relation'], 'ai', 'confirmed' if conf >= AUTO_CONFIRM else 'suggested', 'ai', conf, str(l.get('reason') or ''))
        saved += 1
    return saved


def documents_for(scope, kind, ref, limit=12, ent=None):
    """Topics, in the collections this reader can read, that are linked to an entity (or any of its fields). Grouped by collection, each with its usage tag."""
    keys = [ref] if kind == 'entity' else [ref]
    with conn() as c:
        rows = c.execute("SELECT col.id AS cid, col.name AS collection, col.usage, t.slug, t.title, t.topic_type, t.summary, r.relation, r.kind, r.ref FROM library.topic_model_ref r "
                         "JOIN library.topic t ON t.id=r.topic_id AND t.collection_id=r.collection_id JOIN library.collection col ON col.id=r.collection_id "
                         "WHERE r.status='confirmed' AND t.status='active' AND r.collection_id::text = ANY(%s) AND ((r.kind='entity' AND r.ref=%s) OR (r.kind='field' AND r.ref LIKE %s)) ORDER BY lower(col.name), t.title",
                         (list(scope.grants), ref, ref + '.%')).fetchall()
    out = {}
    for r in rows:
        if scope.level(str(r['cid'])) is None:
            continue
        b = out.setdefault(str(r['cid']), {'collection': r['collection'], 'collection_id': str(r['cid']), 'usage': r['usage'], 'usage_note': S.USAGE_NOTE[r['usage']], 'topics': []})
        if len(b['topics']) < limit and not any(t['topic'] == r['slug'] for t in b['topics']):
            b['topics'].append({'topic': r['slug'], 'title': r['title'], 'type': r['topic_type'], 'summary': r['summary'], 'relation': r['relation'], 'label': LABEL[r['relation']],
                                'about': r['ref'] if r['kind'] == 'field' else None, 'link': f"/library/topics/{r['slug']}?c={r['cid']}"})
    return list(out.values())


def graph_overlay(scope, entity_keys, limit=150):
    """Topics (of the collections this reader can read) that are linked to model entities, as graph nodes and edges, to draw over the model's graph.
    The links between those topics (inside their collection) are drawn too."""
    with conn() as c:
        rows = c.execute("SELECT DISTINCT r.collection_id AS cid, col.name AS collection, t.id, t.slug, t.title, r.ref, r.relation FROM library.topic_model_ref r JOIN library.topic t ON t.id=r.topic_id AND t.collection_id=r.collection_id "
                         "JOIN library.collection col ON col.id=r.collection_id WHERE r.status='confirmed' AND r.kind='entity' AND t.status='active' AND r.collection_id::text = ANY(%s) ORDER BY t.title LIMIT %s", (list(scope.grants), limit * 3)).fetchall()
    nodes, edges, seen = [], [], set()
    for r in rows:
        if scope.level(str(r['cid'])) is None or r['ref'] not in entity_keys:
            continue
        key = f"topic:{r['cid']}:{r['slug']}"
        if key not in seen:
            if len(seen) >= limit:
                continue
            seen.add(key)
            nodes.append({'key': key, 'name': r['title'], 'domain': f"Documents: {r['collection']}", 'kind': 'topic', 'collection': str(r['cid']), 'slug': r['slug']})
        edges.append({'from': key, 'to': r['ref'], 'status': 'confirmed', 'relation': r['relation']})
    ids = {n['key']: n for n in nodes}
    if ids:
        with conn() as c:
            for l in c.execute("SELECT l.collection_id AS cid, f.slug AS fs, t.slug AS ts, l.relation FROM library.topic_link l JOIN library.topic f ON f.id=l.from_topic AND f.collection_id=l.collection_id "
                               "JOIN library.topic t ON t.id=l.to_topic AND t.collection_id=l.collection_id WHERE l.collection_id::text = ANY(%s)", (list({n['collection'] for n in nodes}),)).fetchall():
                a, b = f"topic:{l['cid']}:{l['fs']}", f"topic:{l['cid']}:{l['ts']}"
                if a in ids and b in ids:
                    edges.append({'from': a, 'to': b, 'status': 'optional', 'relation': l['relation']})          # drawn dashed: a link between documents, not to the model
    return {'nodes': nodes, 'edges': edges}


def kinds_of(cid, topic_ids):
    """For topics that are a kind or an instance of a model entity: {topic id: [entity names]}. This is what a topic 'is' according to the governed model."""
    if not topic_ids:
        return {}
    onto = _onto()
    with conn() as c:
        rows = c.execute("SELECT topic_id, ref FROM library.topic_model_ref WHERE collection_id=%s AND topic_id = ANY(%s) AND kind='entity' AND relation='instance_of' AND status='confirmed' ORDER BY ref", (cid, list(topic_ids))).fetchall()
    out = {}
    for r in rows:
        e = (onto or {}).get('entities', {}).get(r['ref'])
        if e:
            out.setdefault(r['topic_id'], []).append(e['name'])
    return out
