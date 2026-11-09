"""From a database catalog (see connectors.introspect) to a DRAFT model: entities from tables, fields from columns, relationships from foreign keys.

Everything generated is marked needs_review: this is a starting point for the modeller, who adds meaning, filters, calculated fields and examples.
Sensitivity is guessed from column names (a hint, never a guarantee), and guessed on the safe side.
"""
import re
from datetime import date

RESTRICTED = re.compile(r'passport|national[_ ]?id|identity[_ ]?(card|no|number)|id[_ ]?card|\bssn\b|social[_ ]?security|credit[_ ]?card|card[_ ]?(no|number)|cvv|iban|password|passwd|secret|api[_ ]?key|token|id[_ ]?number|tax[_ ]?id|licen[sc]e[_ ]?(no|number)')
PERSONAL = re.compile(r'e[_ ]?mail|phone|mobile|first[_ ]?name|last[_ ]?name|full[_ ]?name|surname|given[_ ]?name|birth|dob|gender|address|street|postal|zip|ip[_ ]?address|latitude|longitude')
CONFIDENTIAL = re.compile(r'amount|balance|price|revenue|salary|wage|cost|charge|fee|tax|vat|discount|payment|invoice[_ ]?total|credit[_ ]?limit')
LINEAGE = re.compile(r'^(load_(timestamp|time|date|dt)|etl_\w+|ingested_at|source_system|record_source|_?dbt_\w+|_?airbyte_\w+|_?fivetran_\w+|batch_id)$')
DEFAULT_POLICY = {'internal': 'Return freely to authorized internal users.', 'confidential': 'Financial values; only for roles entitled to customer financials.',
                  'personal': 'Names, emails, phone numbers of persons: masked unless the caller is entitled.', 'restricted': 'Identity numbers, credentials and similar: never returned, not present in agent views.',
                  'row_level': 'Not configured.'}


def snake(s):
    return re.sub(r'[^a-z0-9]+', '_', s.lower()).strip('_') or 'x'


def title(s):
    return ' '.join(w.capitalize() for w in re.split(r'[_\s]+', s) if w)


def map_type(t):
    """A column type from any supported database (PostgreSQL, MySQL/MariaDB, SQLite, DuckDB, ClickHouse) to the model's few types."""
    t = (t or '').lower().strip()
    t = re.sub(r'^(nullable|lowcardinality)\((.*)\)$', r'\2', t)                  # ClickHouse wrappers
    t = re.sub(r'^(nullable|lowcardinality)\((.*)\)$', r'\2', t)
    if t.endswith('[]') or t.startswith('array'):
        return 'text[]'
    if re.match(r'(tinyint\(1\)|bool|bit\(1\))', t):
        return 'boolean'
    if re.match(r'(u?int\d*|u?smallint|u?bigint|u?tinyint|u?mediumint|integer|serial|bigserial|hugeint)', t):
        return 'integer'
    if re.match(r'(numeric|decimal|real|double|float|money|number)', t):
        return 'decimal'
    if t == 'date' or t.startswith('date32'):
        return 'date'
    if t.startswith(('timestamp', 'datetime')):
        return 'timestamp'
    return 'text'


def guess_sensitivity(col):
    n = col.lower()
    if RESTRICTED.search(n):
        return 'restricted'
    if PERSONAL.search(n):
        return 'personal'
    if CONFIDENTIAL.search(n):
        return 'confidential'
    return 'internal'


def access_for(sens):
    return {'restricted': 'none', 'personal': 'masked'}.get(sens, 'full')


def generate(catalog, connector_name, tables=None, domain_from='schema'):
    """-> (model dict in the assembled shape, notes: list of strings). `tables` limits to 'schema.table' names."""
    chosen = [t for t in catalog['tables'] if not tables or f"{t['schema']}.{t['name']}" in set(tables)]
    notes = []
    names = {}                                     # (schema, table) -> entity key; keys must be unique across schemas
    used = set()
    for t in chosen:
        k = snake(t['name'])
        if k in used:
            k = snake(f"{t['schema']}_{t['name']}")
        while k in used:
            k += '_x'
        used.add(k)
        names[(t['schema'], t['name'])] = k
    ents, rels = {}, []
    pk_of = {}
    for t in chosen:
        key = names[(t['schema'], t['name'])]
        pk = t['pk'] or next((u for u in t['uniques'] if len(u) == 1), [])
        if not pk and any(c['name'].lower() == 'id' for c in t['columns']):
            pk = [next(c['name'] for c in t['columns'] if c['name'].lower() == 'id')]
            notes.append(f"{t['schema']}.{t['name']}: no key declared; the column {pk[0]} is assumed to identify a row")
        elif not t['pk'] and not pk:
            pass
        pk_of[(t['schema'], t['name'])] = pk
        if not t['pk'] and not pk:
            notes.append(f"{t['schema']}.{t['name']}: no primary key declared" + (' (a unique column is used instead)' if pk else '; please set one'))
    for t in chosen:
        key = names[(t['schema'], t['name'])]
        pk = pk_of[(t['schema'], t['name'])]
        fk_cols = {c: fk for fk in t['fks'] for c in fk['cols'] if (fk['ref_schema'], fk['ref_table']) in names}
        kind = 'derived' if t['kind'] == 'view' else ('bridge' if len(t['fks']) >= 2 and len(t['columns']) <= len(t['fks']) + 4 else 'master')
        attrs = {}
        for c in t['columns']:
            sens = guess_sensitivity(c['name'])
            role = 'primary_key' if c['name'] in pk else 'foreign_key' if c['name'] in fk_cols else 'lineage' if LINEAGE.match(c['name']) else 'attribute'
            if role in ('primary_key', 'foreign_key', 'lineage') and sens != 'restricted':
                sens = 'internal'
            a = {'role': role, 'type': map_type(c['type']), 'business_name': title(c['name']), 'description': c.get('comment') or '', 'sensitivity': sens,
                 'agent_access': access_for(sens), 'review_status': 'needs_review'}
            if not a['description']:
                a.pop('description')
            if c['name'] in fk_cols:
                fk = fk_cols[c['name']]
                a['references'] = title(names[(fk['ref_schema'], fk['ref_table'])])
            if c.get('values') and sens in ('internal', 'confidential') and role == 'attribute' and a['type'] in ('text', 'boolean'):
                a['allowed_values'] = [str(v) for v in c['values']]
            if sens == 'restricted':
                notes.append(f"{t['schema']}.{t['name']}.{c['name']} looks restricted (by its name): set to agent_access none; confirm")
            attrs[c['name']] = a
        ents[key] = {'id': key, 'name': title(t['name']), 'domain': title(t['schema']), 'kind': kind, 'definition': t.get('comment') or '', 'semantic_table': f"{t['schema']}.{t['name']}",
                     'primary_key': ', '.join(pk) if pk else '', 'business_key': ', '.join(pk) if pk else (t['columns'][0]['name'] if t['columns'] else ''),
                     'system_of_record': connector_name, 'synonyms': [], 'model_status': 'new', 'filters': [], 'checks': [], 'attributes': attrs, 'calculated': {}}
        if not ents[key]['definition']:
            ents[key].pop('definition')
        if not ents[key]['primary_key']:
            ents[key].pop('primary_key')
    # relationships: declared foreign keys first, then obvious undeclared ones (column named <table>_id pointing at a single-column key)
    n = 0
    declared = set()
    for t in chosen:
        for fk in t['fks']:
            tgt = (fk['ref_schema'], fk['ref_table'])
            if tgt not in names or len(fk['cols']) != len(fk['ref_cols']):
                continue
            n += 1
            ck, pk_ = names[(t['schema'], t['name'])], names[tgt]
            nullable = any(next(c for c in t['columns'] if c['name'] == x)['nullable'] for x in fk['cols'])
            join = ' AND '.join(f"{t['name']}.{a} = {fk['ref_table']}.{b}" for a, b in zip(fk['cols'], fk['ref_cols']))
            pred = snake(re.sub(r'_(id|key|no|number)$', '', fk['cols'][0])) if len(fk['cols']) == 1 else f"refers_to_{snake(fk['ref_table'])}"
            rels.append({'id': f'R-{n:03d}', 'from': ents[ck]['name'], 'predicate': pred, 'to': ents[pk_]['name'], 'cardinality': 'many-to-zero-or-one (optional)' if nullable else 'many-to-one',
                         'join': join, 'status': 'confirmed', 'status_detail': 'Declared foreign key', 'sentence': f"Each {ents[ck]['name']} refers to {'at most one' if nullable else 'exactly one'} {ents[pk_]['name']} via {', '.join(fk['cols'])}."})
            declared.update((t['schema'], t['name'], c) for c in fk['cols'])
    single_pk = {(s, tb): p[0] for (s, tb), p in pk_of.items() if len(p) == 1}
    by_table = {}
    for (s, tb), p in single_pk.items():
        by_table.setdefault(tb.lower(), []).append((s, tb, p))
    for t in chosen:
        for c in t['columns']:
            if (t['schema'], t['name'], c['name']) in declared or c['name'] in pk_of[(t['schema'], t['name'])]:
                continue
            m = re.match(r'^(.+?)_(id|key)$', c['name'].lower())
            cand = by_table.get(m.group(1)) if m else None
            if not cand or len(cand) != 1:
                continue
            s, tb, p = cand[0]
            if (s, tb) == (t['schema'], t['name']):
                continue
            ck, pk_ = names[(t['schema'], t['name'])], names[(s, tb)]
            n += 1
            rels.append({'id': f'R-{n:03d}', 'from': ents[ck]['name'], 'predicate': snake(m.group(1)), 'to': ents[pk_]['name'], 'cardinality': 'many-to-one', 'join': f"{t['name']}.{c['name']} = {tb}.{p}",
                         'status': 'unreliable', 'status_detail': 'Inferred from the column name: no foreign key is declared', 'sentence': f"{ents[ck]['name']} probably refers to {ents[pk_]['name']} via {c['name']} (not a declared key)."})
            ents[ck]['attributes'][c['name']].update(role='foreign_key', references=ents[pk_]['name'])
            notes.append(f"{t['schema']}.{t['name']}.{c['name']}: link to {ents[pk_]['name']} inferred from the name, not declared (status unreliable)")
    model = {'ontology': {'name': f'{connector_name} (from database)', 'id': snake(connector_name), 'version': 'draft', 'date': date.today().isoformat(),
                          'source': f'Introspected from connector {connector_name}', 'status': 'Generated: every item needs review.'},
             'access_policy': DEFAULT_POLICY, 'entities': ents, 'relationships': rels,
             'examples': []}
    return model, notes
