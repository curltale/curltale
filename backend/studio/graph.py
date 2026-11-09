"""Navigation over the entity graph: neighbours and join paths (weighted by how reliable each link is)."""
import heapq
from .db import conn

# lower = preferred: confirmed links first; optional links may be empty; unreliable ones exist but are often empty or wrong
WEIGHT = {'confirmed': 1.0, 'optional': 3.0, 'unreliable': 6.0}
WARN = {'optional': 'optional link: it may be empty for some records', 'unreliable': 'unreliable link: it exists but is often empty or wrong'}


def load(vid):
    """Entities (key, name, domain, data{kind, synonyms}) and relationships (as dicts: id, from, to, predicate, cardinality, join, status, note...)."""
    with conn() as c:
        ents = c.execute('SELECT key,name,domain,kind,synonyms FROM entity WHERE version_id=%s ORDER BY position', (vid,)).fetchall()
        rels = c.execute('SELECT * FROM relationship WHERE version_id=%s ORDER BY position', (vid,)).fetchall()
    ents = [{'key': e['key'], 'name': e['name'], 'domain': e['domain'], 'data': {'kind': e['kind'], 'synonyms': e['synonyms'] or []}} for e in ents]
    out = []
    for r in rels:
        d = {'id': r['rid'], 'from': r['from_entity'], 'to': r['to_entity'], 'predicate': r['predicate'], 'cardinality': r['cardinality'], 'join': r['join_cond'], 'status': r['status']}
        d.update(r['extra'] or {})
        out.append(d)
    return ents, out


def resolve_entity(vid, text, ents=None):
    """Find an entity key from a key, name, name without brackets, or synonym (case-insensitive)."""
    ents = ents or load(vid)[0]
    t = text.strip().lower()
    for e in ents:
        if t == e['key'].lower() or t == e['name'].lower():
            return e['key']
    for e in ents:
        if t == e['name'].split(' (')[0].lower():
            return e['key']
    for e in ents:
        if t in [s.lower() for s in e['data'].get('synonyms', [])]:
            return e['key']
    return None


def _edges(vid):
    ents, rels = load(vid)
    name_key = {e['name']: e['key'] for e in ents}
    edges = []
    for r in rels:
        a, b = name_key.get(r['from']), name_key.get(r['to'])
        edges.append({'rid': r['id'], 'from': a, 'to': b, 'from_name': r['from'], 'to_name': r['to'], 'predicate': r['predicate'],
                      'cardinality': r.get('cardinality'), 'join': r['join'], 'status': r['status'], 'note': r.get('note')})
    return ents, edges


def neighbors(vid, key):
    ents, edges = _edges(vid)
    out = []
    for e in edges:
        if e['from'] == key:
            out.append({**e, 'direction': 'out', 'other': e['to'], 'other_name': e['to_name']})
        elif e['to'] == key:
            out.append({**e, 'direction': 'in', 'other': e['from'], 'other_name': e['from_name']})
    return out


def graph(vid):
    """Nodes and edges for the UI graph view."""
    ents, edges = _edges(vid)
    return {'nodes': [{'key': e['key'], 'name': e['name'], 'domain': e['domain'], 'kind': e['data'].get('kind')} for e in ents],
            'edges': [e for e in edges if e['from'] and e['to']]}


def _up(cardinality, forward):
    """True when this hop moves from the 'many' side to the 'one' side (towards a parent), False for the opposite, None if level."""
    c = (cardinality or '').lower()
    if c.startswith('many-to'):
        return forward
    if c.startswith('one-to-many'):
        return not forward
    return None


FAN_OUT_PENALTY = 4.0


def _dijkstra(edges, src, dst, allowed, allow_fan_out=True):
    """Cheapest path over states (entity, direction of the previous hop).
    Going up to a shared parent and back down (A -> parent <- B) joins sibling records: it matches every B of that
    parent, not the specific one. It is allowed (with a penalty and a warning) unless allow_fan_out is False."""
    adj = {}
    for e in edges:
        if not e['from'] or not e['to'] or e['status'] not in allowed or e['from'] == e['to']:
            continue
        w = WEIGHT.get(e['status'], 5.0)
        adj.setdefault(e['from'], []).append((e['to'], w, e, True))
        adj.setdefault(e['to'], []).append((e['from'], w, e, False))
    start = (src, None)
    pq, best, prev = [(0.0, 0, start)], {start: 0.0}, {}
    tie, goal = 0, None
    while pq:
        d, _, st = heapq.heappop(pq)
        u, last = st
        if u == dst:
            goal = st
            break
        if d > best.get(st, 1e9):
            continue
        for v, w, e, forward in adj.get(u, []):
            up = _up(e['cardinality'], forward)
            turn = last is True and up is False
            if turn and not allow_fan_out:
                continue
            nd = d + w + (FAN_OUT_PENALTY if turn else 0)
            ns = (v, up if up is not None else last)
            if nd < best.get(ns, 1e9):
                best[ns], prev[ns] = nd, (st, e, forward, turn, up)
                tie += 1
                heapq.heappush(pq, (nd, tie, ns))
    if goal is None:
        return None
    if goal == start:
        return {'cost': 0, 'hops': [], 'all_confirmed': True, 'fan_out': False}
    hops, cur = [], goal
    while cur != start:
        pst, e, forward, turn, up = prev[cur]
        h = {'from': pst[0], 'to': cur[0], 'predicate': e['predicate'], 'followed': 'forward' if forward else 'reverse',
             'direction': {True: 'up', False: 'down', None: 'level'}[up], 'cardinality': e['cardinality'], 'join': e['join'],
             'status': e['status'], 'rid': e['rid'], 'warning': WARN.get(e['status'])}
        if turn:
            h['fan_out'] = True
            h['warning'] = ((h['warning'] + '; ') if h['warning'] else '') + (
                f"FAN-OUT: both sides hang off the same parent ({pst[0]}), so this matches every sibling record of that parent, "
                "not the specific related one")
        hops.append(h)
        cur = pst
    hops.reverse()
    return {'cost': best[goal], 'hops': hops, 'all_confirmed': all(h['status'] == 'confirmed' for h in hops),
            'fan_out': any(h.get('fan_out') for h in hops)}


def find_path(vid, a, b):
    """Best join path between two entities. Returns the preferred path and, if different, the all-confirmed one."""
    ents, edges = _edges(vid)
    ka, kb = resolve_entity(vid, a, ents), resolve_entity(vid, b, ents)
    if not ka or not kb:
        return {'error': f"unknown entity: {a if not ka else b}"}
    if ka == kb:
        return {'from': ka, 'to': kb, 'best': {'cost': 0, 'hops': [], 'all_confirmed': True}}
    names = {e['key']: e['name'] for e in ents}
    best = _dijkstra(edges, ka, kb, set(WEIGHT))
    strict = _dijkstra(edges, ka, kb, {'confirmed'}, allow_fan_out=False)       # every hop confirmed AND a real chain, no sibling join
    res = {'from': ka, 'to': kb, 'best': best}
    if strict and best and strict['hops'] != best['hops']:
        res['confirmed_only'] = strict
    if best and not (best['all_confirmed'] and not best['fan_out']) and not strict:
        approx = _dijkstra(edges, ka, kb, {'confirmed'})
        if approx and approx['fan_out']:
            res['approximate_via_shared_parent'] = approx
            res['note'] = ('No fully confirmed chain exists. The only all-confirmed route goes through a shared parent and matches every sibling '
                           'record, so it can describe "same parent" but not "this specific record".')
    if best is None:
        res['note'] = 'the model has no join path between these entities'
    for p in (res.get('best'), res.get('confirmed_only'), res.get('approximate_via_shared_parent')):
        if p:
            for h in p['hops']:
                h['from_name'], h['to_name'] = names[h['from']], names[h['to']]
    return res
