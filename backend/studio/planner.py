"""Query planning from the semantic layer: from a question and a set of entities to everything needed for ONE SQL statement.

The point: an agent should not discover the model with a dozen small calls and then merge partial results in its head.
It should get tables, columns, join keys, governed definitions and a runnable skeleton at once, and let the database do
the joining and aggregating. Aggregating each one-to-many branch before joining (the skeleton does this) avoids the
fan-out trap; the model's named filters say what 'open', 'active' or 'unpaid' mean instead of guessing values.
"""
import re

from . import access, graph, modelview, search as S
from .synth import ddl

JOIN = re.compile(r'(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)')
ROLE_ORDER = {'primary_key': 0, 'foreign_key': 1, 'dimension': 2, 'fact_measure': 3, 'derived_attribute': 4}
MAX_COLS = 12
MAX_COLS_GRAIN = 14


def _short(s, n=90):
    s = ' '.join(str(s or '').split())
    return s if len(s) <= n else s[:n - 1] + '…'


def _words(text):
    return {w[:-1] if w.endswith('s') and len(w) > 4 else w for w in re.split(r'[^a-z0-9]+', (text or '').lower()) if len(w) > 2}


def _columns(e, ent, question='', cap=MAX_COLS):
    """Columns the caller may use, most useful first: those the question talks about, then keys, foreign keys, dimensions and measures, enumerations."""
    rows, qw = [], _words(question)
    for c, a in e['attributes'].items():
        if a['role'] == 'lineage' or not access.can_see(a.get('sensitivity'), a.get('agent_access'), ent):
            continue
        rank = ROLE_ORDER.get(a['role'], 5 if a.get('allowed_values') else 6 if a['type'] in ('date', 'timestamp') else 7)
        if qw and a['role'] != 'primary_key' and (_words(c.replace('_', ' ')) & qw or {str(v).lower() for v in a.get('allowed_values', [])} & qw):
            rank = -1                       # the question mentions this column or one of its values (e.g. 'premium', 'active')
        tag = {'primary_key': ' PK', 'foreign_key': ' FK->' + (a.get('references') or '').split(' (')[0], 'dimension': ' dim', 'fact_measure': ' measure'}.get(a['role'], '')
        d = f"{c} {a['type']}{tag} - {_short(a.get('description') or a['business_name'], 60)}"
        if a.get('allowed_values'):
            d += ' | values: ' + ' / '.join(a['allowed_values'][:9]) + (' / ...' if len(a['allowed_values']) > 9 else '')
        if a['review_status'] in ('needs_review', 'source_needed'):
            d += ' | (pending confirmation)'
        rows.append((rank, c, d))
    rows.sort(key=lambda r: (r[0], r[1]))
    cols = [d for _, _, d in rows]
    return cols[:cap], max(0, len(cols) - cap)


def _table(e):
    return ddl.qualified(e).replace('"', '')


def _alias(e):
    return ddl.table_of(e)


def _name_col(e, ent):
    for c, a in e['attributes'].items():
        if c.endswith('_name') and access.can_see(a.get('sensitivity'), a.get('agent_access'), ent) and a['type'] == 'text':
            return c
    return None


MEASURE_HINTS = ('due', 'balance', 'total', 'amount', 'revenue', 'charges', 'charge', 'paid', 'received', 'value')


def _measure_cols(e, ent, limit=3):
    """Numeric columns worth summing, best first: columns with the role fact_measure, then names that sound like amounts."""
    out = []
    for c, a in e['attributes'].items():
        if a['role'] != 'lineage' and (a['type'].startswith('decimal') or a['role'] == 'fact_measure') and 'ratio' not in a['type'] and a.get('unit') != 'ratio' \
                and access.can_see(a.get('sensitivity'), a.get('agent_access'), ent):
            tokens = set(re.split(r'[_\W]+', (c + ' ' + (a.get('business_name') or '') + ' ' + (a.get('unit') or '')).lower()))
            hint = next((i for i, h in enumerate(MEASURE_HINTS) if h in tokens), None)      # whole words of the column name, business name or unit: 'chargeable' is not money
            if hint is not None or a['role'] == 'fact_measure':
                out.append(((0 if a['role'] == 'fact_measure' else 1), hint if hint is not None else 99, c))
    return [c for _, _, c in sorted(out)][:limit]


def derive_filters(onto, question):
    """Filters the MODEL defines for the words of the question, per table: {table: (predicate, label)}.
    A filter is a named condition on an entity's table (entity.filters, editable on the entity page) with synonyms. It applies only when ALL the words of
    its name or of one synonym are in the question ('open' + 'order'), never on a shared word like 'customer'. The best match per table wins; a tie
    means the question is not specific enough, so nothing is pre-filled."""
    qw = _words(question)
    if not qw:
        return {}
    cands = {}                                                                   # table -> [(score, predicate, label)]
    for key, f in modelview.filters(onto):
        if not str(f.get('filter', '')).strip():
            continue
        score = max((len(w) for w in (_words(p.replace('_', ' ')) for p in [f['name']] + list(f.get('synonyms', []))) if w and w <= qw), default=0)
        if score:
            pred = f['filter'].strip()
            label = f"{f.get('status', 'working')} definition '{f['name']}'" + (f" ({f['source']})" if f.get('source') else '')
            cands.setdefault(_table(onto['entities'][key]), []).append((score, f'({pred})' if ' OR ' in pred.upper() else pred, label))
    out = {}
    for table, cs in cands.items():
        cs.sort(key=lambda c: -c[0])
        if len(cs) == 1 or cs[0][0] > cs[1][0]:
            out[table] = (cs[0][1], cs[0][2])
    return out


def _link(onto, vid, ents, fks, grain, target, ent):
    """How a target entity attaches to the grain entity."""
    ge, te = ents[grain], ents[target]
    direct = [c for k, c, t in fks if k == target and t == grain]
    if direct:
        col = direct[0]
        status = 'confirmed'
        for r in onto['relationships']:           # the model says how far this particular link can be trusted
            m = JOIN.search(r['join'])
            if m and r['from'] == te['name'] and r['to'] == ge['name'] and col in (m.group(2), m.group(4)):
                status = r['status']
                break
        link = {'kind': 'branch_by_key', 'key': col, 'from': f'{_table(te)}.{col}', 'status': status}
        if status != 'confirmed':
            link['warning'] = f"the model marks the link {_alias(te)}.{col} as {status}: it can be empty or wrong, so a missing match proves nothing"
        return link
    p = graph.find_path(vid, ge['name'], te['name'])
    best = p.get('best') if isinstance(p, dict) else None
    if not best or not best['hops']:
        return {'kind': 'unreachable', 'why': f"no join path from {ge['name']} to {te['name']} in the model"}
    hops = best['hops']
    link = {'kind': 'chain', 'hops': [{'via': f"{h['from_name']} -> {h['to_name']}", 'join': h['join'], 'status': h['status'], 'direction': h.get('direction')} for h in hops],
            'fan_out': best.get('fan_out', False), 'all_confirmed': best['all_confirmed']}
    if all(h.get('direction') == 'up' for h in hops):
        link['kind'] = 'join_up'                       # towards the 'one' side: a plain LEFT JOIN, no aggregation needed
    link['_hops'] = hops
    return link


def _branch_sql(ents, grain, target, link, key_name, ent, filters=None):
    te = ents[target]
    filt = (filters or {}).get(_table(te))
    where = f"WHERE {filt[0]}   -- {filt[1][:100]}" if filt else f"-- WHERE <filter on {_alias(te)} columns>"
    alias = _alias(te)
    measures = _measure_cols(te, ent)
    sel = f"count(*) AS n" + ''.join(f",\n         sum({m}) AS sum_{m}" for m in measures[:3])
    if link['kind'] == 'branch_by_key':
        body = f"SELECT {link['key']} AS root_id, {sel}\n       FROM {_table(te)}\n       {where}\n       GROUP BY {link['key']}"
    else:
        hops = link['_hops']
        tables = [ents[grain]] + [next(e for e in ents.values() if e['name'] == h['to_name']) for h in hops]
        m = JOIN.search(hops[0]['join'])
        if not m:
            return None
        t1, c1, t2, c2 = m.groups()
        root_ref = f'{t1}.{c1}' if t1 != _alias(ents[grain]) else f'{t2}.{c2}'
        joins = ''.join(f"\n       JOIN {_table(tables[i])} AS {_alias(tables[i])} ON {hops[i]['join']}" for i in range(len(hops) - 1, 0, -1))
        body = (f"SELECT {root_ref} AS root_id, {sel}\n       FROM {_table(te)} AS {alias}{joins}\n       {where}\n       GROUP BY {root_ref}")
    return f"LEFT JOIN (\n       {body}\n) AS {alias} ON {alias}.root_id = {_alias(ents[grain])}.{ddl.pk_col(ents[grain])}"


def skeleton(ents, grain, links, ent, filters=None):
    ge = ents[grain]
    galias, gpk = _alias(ge), ddl.pk_col(ge)
    cols = [f'{galias}.{gpk}'] + ([f'{galias}.{_name_col(ge, ent)}'] if _name_col(ge, ent) else [])
    joins, extra = [], []
    for target, link in links.items():
        te = ents[target]
        al = _alias(te)
        if link['kind'] in ('branch_by_key', 'chain'):
            j = _branch_sql(ents, grain, target, link, None, ent, filters)
            if j:
                joins.append(j)
                extra.append(f'coalesce({al}.n, 0) AS n_{al}')
        elif link['kind'] == 'join_up':
            hops = link['_hops']
            for h in hops:
                tgt = next(e for e in ents.values() if e['name'] == h['to_name'])
                joins.append(f"LEFT JOIN {_table(tgt)} AS {_alias(tgt)} ON {h['join']}")
            nc = _name_col(te, ent)
            extra.append(f'{al}.{nc or ddl.pk_col(te)} AS {al}_{nc or "id"}')
    sel = ',\n       '.join(cols + extra)
    return (f"SELECT {sel}\nFROM {_table(ge)} AS {galias}\n" + '\n'.join(joins) +
            f"\n-- WHERE {galias}.{gpk} = '<id>'" + (f"   -- or by name: {galias}.{_name_col(ge, ent)} ILIKE '%<text>%'" if _name_col(ge, ent) else '   -- or any filter on the grain columns') + "\n"
            f"ORDER BY 1\nLIMIT 50")


# Heuristic, not model knowledge: entity names that are also ordinary words in a question. A mention of these never adds an entity to the plan
# ('in order of ...' must not add the Order entity). Tune here if a model has other such names.
NOT_A_CUE = {'order', 'case', 'document', 'site', 'project', 'account', 'person', 'payment', 'address', 'item', 'record', 'type'}   # everyday words that also name entities in many models


def mentioned_entities(onto, question, have):
    """Entities the question names that the caller did not list: by entity name or synonym ('orders', 'billing'), or through a named filter
    ('monthly rental' is a filter of Account Rate Plan). Returns [(entity key, the phrase the user used)]."""
    ents = onto['entities']
    qw = _words(question)
    q = ' ' + ' '.join(re.split(r'[^a-z0-9]+', question.lower())) + ' '
    out = []
    for k, e in ents.items():
        if k in have:
            continue
        said = None
        terms = {e['name'].split(' (')[0].lower()} | {x.lower() for x in e.get('synonyms', []) if len(x) >= 5 and ' ' not in x.strip()}
        for t in terms:
            plain = ' '.join(re.split(r'[^a-z0-9]+', t)).strip()
            if plain and plain.split()[-1] not in NOT_A_CUE and re.search(rf' {re.escape(plain)}(s|es)? ', q):
                said = plain
                break
        if not said:
            for f in e.get('filters', []):
                said = next((p for p in [f['name']] + list(f.get('synonyms', [])) if _words(p.replace('_', ' ')) and _words(p.replace('_', ' ')) <= qw), None)
                if said:
                    break
        if said:
            out.append((k, said))
    return out


def domain_hints(onto, question, keys):
    """The question names a topic (a domain of the model, e.g. 'billing') but none of that domain's entities is in the plan: say where that topic lives."""
    q = ' ' + ' '.join(re.split(r'[^a-z0-9]+', question.lower())) + ' '
    ents, out = onto['entities'], []
    for name, members in modelview.domains(onto).items():
        words = [w for w in re.split(r'[^a-z0-9]+', name.lower()) if w]
        if words and members and not any(k in keys for k in members) and all(f' {w}' in q for w in words) \
                and any(re.search(rf' {re.escape(w)}(s|es)? ', q) for w in words) and len(members) > 1:
            out.append(f"the question mentions '{name.lower()}': in this model that topic is the domain {name}. "
                       f"None of its entities is in your list; if you need that information add the right one: {', '.join(ents[k]['name'] for k in members[:7])}")
    return out


def supertype_note(e):
    """A supertype table unifies several subtypes behind a discriminator column: counting it counts all of them."""
    subs = e.get('subtypes') or []
    disc = next((c for c, a in e['attributes'].items() if a.get('allowed_values') and len(a['allowed_values']) == len(subs)), None) if subs else None
    if not subs:
        return None
    vals = e['attributes'][disc]['allowed_values'] if disc else []
    return (f"{e['name']} is a supertype: it unifies {', '.join(subs)}. Counting it counts ALL of them. "
            + (f"To count one kind filter {disc} IN ({', '.join(repr(v) for v in vals)}); " if disc else '')
            + 'for one kind prefer that kind\'s own entity and its governed rule.')


def pick_grain(fks, keys):
    """The entity the others hang off: the one most of them carry a foreign key to."""
    score = {k: sum(1 for x, c, t in fks if t == k and x in keys and x != k) for k in keys}
    return max(keys, key=lambda k: (score[k], -keys.index(k)))


def plan(onto, vid, resolve, entities, grain, question, ent):
    ents = onto['entities']
    fks = ddl.foreign_keys(onto)
    keys, notes = [], []
    for name in entities or []:
        k = resolve(name)
        if k and k not in keys:
            keys.append(k)
        elif not k:
            notes.append(f'unknown entity "{name}" ignored: use describe or search')
    related = []
    if question:
        hits = S.search(vid, question, types=['calculated', 'example', 'filter', 'entity'], k=10, expand=False, ent=ent)
        if not keys:
            keys = [h['id'].split(':', 1)[1] for h in hits if h['type'] == 'entity'][:4]
            if keys:
                notes.append('entities were inferred from the question: check they are what you need')
        related = hits
    if question and keys:                       # a word the model knows ('orders', 'billing', 'monthly fee') brings its entity into the plan
        for k, said in mentioned_entities(onto, question, keys)[:3]:
            if len(keys) < 7:
                keys.append(k)
                notes.append(f"the question says '{said}': that is {ents[k]['name']} ({_table(ents[k])}), which was added to the plan; remove it from your SQL if you do not need it")
    if not keys:
        return {'error': 'no entities: pass the entities involved (e.g. ["customer", "invoice"]) and/or the question', 'hint': 'describe() with no name lists the entities'}
    g = resolve(grain) if grain else None
    g = g or pick_grain(fks, keys)
    if g not in keys:
        keys.insert(0, g)
    links = {t: _link(onto, vid, ents, fks, g, t, ent) for t in keys if t != g}
    filters = derive_filters(onto, question or '')
    warnings = []
    for t, l in links.items():
        if l.get('warning'):
            warnings.append(f"{ents[t]['name']}: {l['warning']}")
        if l['kind'] == 'unreachable':
            warnings.append(l['why'])
        elif l.get('fan_out'):
            warnings.append(f"{ents[t]['name']}: reached through a shared parent (fan-out): it matches every sibling record, not the specific one. Prefer another grain.")
        elif l['kind'] == 'chain' and not l['all_confirmed']:
            warnings.append(f"{ents[t]['name']}: the join path uses links that are optional or unreliable (status in the hops): a missing link is not evidence that nothing exists.")
    if question:
        notes.extend(domain_hints(onto, question, keys))
    for k in keys:
        n = supertype_note(ents[k])
        if n:
            warnings.append(n)
    entity_info = []
    ge = ents[g]
    cols, more = _columns(ge, ent, question, MAX_COLS_GRAIN)
    entity_info.append({'entity': g, 'role': 'GRAIN: one result row per row of this table', 'table': _table(ge), 'primary_key': ddl.pk_col(ge), 'columns': cols,
                        **({'more_columns': more} if more else {})})
    for t in keys:
        if t == g:
            continue
        te, l = ents[t], links[t]
        cols, more = _columns(te, ent, question)
        info = {'entity': t, 'table': _table(te), 'joins_to_grain': (f"by key {l['key']} [{l['status']}]" if l['kind'] == 'branch_by_key' else l['kind'] + ': ' + ' ; '.join(h['join'] + ' [' + h['status'] + ']' for h in l.get('hops', []))), 'columns': cols}
        if more:
            info['more_columns'] = more
        entity_info.append(info)
    names = {ents[k]['name'] for k in keys}
    q = (question or '').lower()
    allc = modelview.calculated(onto)

    def relevance(item):
        c, k = item
        words = {w for w in re.split(r'[_\W]+', c.lower() + ' ' + k['name'].lower()) if len(w) > 2}
        return -sum(1 for w in words if w in q)                                  # words from the question first
    calculated = [{'field': f"{k['entity_key']}.{c}", 'name': k['name'], 'entity': k['entity'], 'description': _short(k.get('description') or '', 220),
                   **({'sql': ' '.join(k['sql'].split())[:420]} if k.get('sql') else {}), **({'unit': k['unit']} if k.get('unit') else {})}
                  for c, k in sorted(((c, k) for c, k in allc.items() if (k['entity'] in names or relevance((c, k)) < 0) and access.can_see(k.get('sensitivity'), k.get('agent_access'), ent)),
                                     key=relevance)][:6]
    patterns = [{'question': x['title'], 'sql': ' '.join(x['sql'].split())[:420]} for x in modelview.examples_for(onto, keys)][:3]
    try:                                                          # what the documents say about these entities and this question (brief; the agent opens what it needs)
        from .library import agent as _lib
        documents = _lib.documents_for_question(question or '', keys)
    except Exception:
        documents = []
    return {
        'grain': {'entity': g, 'table': _table(ge), 'meaning': f"one result row per {ge['name'].split(' (')[0]}"},
        'sql_skeleton': skeleton(ents, g, links, ent, filters),
        'how_to_use_it': [
            'Write ONE SQL statement from this skeleton and run it with query_data: the database joins, filters, aggregates and ranks; do not fetch rows and combine them yourself.',
            'Each one-to-many branch is aggregated in its own subquery keyed by root_id and LEFT JOINed to the grain (as in the skeleton): never join several one-to-many tables flat, sums and counts would multiply.',
            "Put the filters and aggregates the question needs inside each branch. 'values' in the column lists are the exact spellings.",
            'Use the filters already in the skeleton for words like open, active, unpaid instead of guessing status values. Calculated fields below are not columns: their SQL (when given) says how they are computed.',
            'List columns explicitly (no SELECT *): you only see the columns your credential is entitled to.'],
        'calculated_fields': calculated,
        'warnings': warnings + notes,
        'entities': entity_info,
        'model_query_patterns': patterns,
        **({'documents': documents, 'documents_note': 'Topics from the document collections you may read that are about these entities or match the question. Open one with library_topic when it can change how you build the query (a definition, a rule, an exception).'} if documents else {}),
    }
