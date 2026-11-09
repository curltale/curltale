"""Edits on the draft version. Every change is validated lightly and written to the audit log.

The model has four concepts: entities (with their filters, checks, synonyms), fields (attributes and calculated fields), relationships, examples.
"""
from . import ontology as O
from .chunker import build_chunks
from .db import conn, J

SENSITIVITY = {'internal', 'confidential', 'personal', 'restricted'}
AGENT_ACCESS = {'full', 'masked', 'none'}
REVIEW = {'confirmed_by_team', 'by_design', 'high_confidence', 'needs_review', 'source_needed'}
REL_STATUS = set(O.REL_STATUS)
FILTER_STATUS = {'governed', 'working'}
FIELD_REQUIRED = ['business_name', 'type', 'role', 'sensitivity']
ENTITY_REQUIRED = ['name', 'kind', 'domain', 'semantic_table', 'business_key', 'system_of_record']     # an industry-standard mapping (tmf) is optional
REL_REQUIRED = ['from', 'to', 'predicate', 'cardinality', 'join', 'status']


class EditError(ValueError):
    pass


def ref_ok(ref, names):
    """An entity reference may carry a trailing note in brackets, or list alternatives ("Person / Organisation")."""
    known = set(names) | {n.split(' (')[0] for n in names}
    base = ref.split(' (')[0].strip() if ref not in known else ref
    return base in known or all(p.strip() in known for p in base.split(' / '))


def draft(c):
    r = c.execute("SELECT id FROM ontology_version WHERE status='draft'").fetchone()
    if not r:
        raise EditError('no draft version: create a draft first')
    return r['id']


def _audit(c, user, vid, action, kind, key, before, after):
    c.execute('INSERT INTO audit_log (username,version_id,action,kind,key,before,after) VALUES (%s,%s,%s,%s,%s,%s,%s)', (user, vid, action, kind, key, J(before), J(after)))


def _need(d, fields, what):
    miss = [f for f in fields if d.get(f) in (None, '')]
    if miss:
        raise EditError(f'{what}: missing {", ".join(miss)}')


def with_defaults(f):
    """A field as stored: agent access follows the sensitivity unless set (restricted -> none), review status defaults to needs_review."""
    f = dict(f)
    if not f.get('agent_access'):
        f['agent_access'] = 'none' if f.get('sensitivity') == 'restricted' else 'full'
    if not f.get('review_status'):
        f['review_status'] = 'needs_review'
    if f.get('role') == 'calculated' and not f.get('type'):
        f['type'] = 'decimal'
    return f


def _check_field(f):
    what = 'calculated field' if f.get('role') == 'calculated' else 'field'
    _need(f, FIELD_REQUIRED, what)
    for k, allowed in (('sensitivity', SENSITIVITY), ('agent_access', AGENT_ACCESS), ('review_status', REVIEW)):
        if f[k] not in allowed:
            raise EditError(f'{what}: {k} must be one of {sorted(allowed)}')
    if f['sensitivity'] == 'restricted' and f['agent_access'] != 'none':
        raise EditError(f'{what}: a restricted field must have agent_access none')
    if f.get('allowed_values') is not None and not isinstance(f['allowed_values'], list):
        raise EditError(f'{what}: allowed_values must be a list')
    if f.get('custom') is not None and not (isinstance(f['custom'], dict) and all(isinstance(v, str) for v in f['custom'].values())):
        raise EditError(f'{what}: custom properties must be name: text pairs')


def _check_entity_lists(e):
    names = set()
    for f in e.get('filters', []):
        _need(f, ['name', 'filter'], 'filter')
        if f.get('status') and f['status'] not in FILTER_STATUS:
            raise EditError(f"filter '{f['name']}': status must be governed or working")
        if f['name'].lower() in names:
            raise EditError(f"filter '{f['name']}' appears twice on this entity")
        names.add(f['name'].lower())
    ids = set()
    for c in e.get('checks', []):
        _need(c, ['id', 'rule'], 'check')
        if c['id'] in ids:
            raise EditError(f"check {c['id']} appears twice on this entity")
        ids.add(c['id'])
    if not isinstance(e.get('synonyms', []), list):
        raise EditError('synonyms must be a list')


# ---------- entities
def _entity(c, vid, key):
    r = c.execute('SELECT * FROM entity WHERE version_id=%s AND key=%s', (vid, key)).fetchone()
    if not r:
        raise EditError(f'entity {key} not found')
    e = {c_: r[c_] for c_ in O.ENTITY_COLS if r[c_] is not None}
    e.update(r['extra'] or {})
    for l in O.ENTITY_LISTS:
        e[l] = r[l] or []
    return e


def upsert_entity(user, key, patch, create=False):
    with conn() as c:
        vid = draft(c)
        exists = c.execute('SELECT 1 FROM entity WHERE version_id=%s AND key=%s', (vid, key)).fetchone()
        if create and exists:
            raise EditError(f'entity {key} already exists')
        if not create and not exists:
            raise EditError(f'entity {key} not found')
        old = None if create else _entity(c, vid, key)
        data = {**(old or {'synonyms': [], 'filters': [], 'checks': []}), **patch}
        for k in O.ENTITY_DERIVED:
            data.pop(k, None)
        _need(data, ENTITY_REQUIRED, 'entity')
        if data.get('tmf') is not None and not isinstance(data['tmf'], dict):
            raise EditError('entity: the standard mapping (tmf) must be an object such as {"resource": ..., "reference": ...}')
        _check_entity_lists(data)
        cols, lists, extra = O.entity_row(key, data)
        if create:
            pos = c.execute('SELECT coalesce(max(position),-1)+1 AS p FROM entity WHERE version_id=%s', (vid,)).fetchone()['p']
            c.execute('INSERT INTO entity (version_id,key,position,name,domain,kind,definition,semantic_table,primary_key,business_key,system_of_record,synonyms,filters,checks,extra) '
                      'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', (vid, key, pos, *cols, *[J(x) for x in lists], J(extra)))
        else:
            if data['name'] != old['name']:
                # relationships refer to entities by name: keep them consistent
                c.execute('UPDATE relationship SET from_entity=%s WHERE version_id=%s AND from_entity=%s', (data['name'], vid, old['name']))
                c.execute('UPDATE relationship SET to_entity=%s WHERE version_id=%s AND to_entity=%s', (data['name'], vid, old['name']))
            c.execute('UPDATE entity SET name=%s,domain=%s,kind=%s,definition=%s,semantic_table=%s,primary_key=%s,business_key=%s,system_of_record=%s,synonyms=%s,filters=%s,checks=%s,extra=%s '
                      'WHERE version_id=%s AND key=%s', (*cols, *[J(x) for x in lists], J(extra), vid, key))
        _audit(c, user, vid, 'create' if create else 'update', 'entity', key, old, data)
        return data


def delete_entity(user, key):
    with conn() as c:
        vid = draft(c)
        e = _entity(c, vid, key)
        used = c.execute('SELECT count(*) AS n FROM relationship WHERE version_id=%s AND (from_entity=%s OR to_entity=%s)', (vid, e['name'], e['name'])).fetchone()['n']
        if used:
            raise EditError(f'entity {key} is used by {used} relationship(s): delete or re-point them first')
        c.execute('DELETE FROM entity WHERE version_id=%s AND key=%s', (vid, key))
        _audit(c, user, vid, 'delete', 'entity', key, e, None)


# ---------- fields (attributes and calculated fields live in one table; a calculated field has role 'calculated' and no physical column)
def upsert_field(user, entity_key, col, data):
    data = with_defaults(data)
    _check_field(data)
    with conn() as c:
        vid = draft(c)
        _entity(c, vid, entity_key)
        old = c.execute('SELECT * FROM field WHERE version_id=%s AND entity_key=%s AND col=%s', (vid, entity_key, col)).fetchone()
        cols, av, extra = O.field_row(data)
        if old:
            c.execute('UPDATE field SET role=%s,type=%s,business_name=%s,description=%s,sensitivity=%s,agent_access=%s,review_status=%s,business_term=%s,allowed_values=%s,extra=%s '
                      'WHERE version_id=%s AND entity_key=%s AND col=%s', (*cols, J(av) if av is not None else None, J(extra), vid, entity_key, col))
        else:
            pos = c.execute('SELECT coalesce(max(position),-1)+1 AS p FROM field WHERE version_id=%s AND entity_key=%s', (vid, entity_key)).fetchone()['p']
            c.execute('INSERT INTO field (version_id,entity_key,col,position,role,type,business_name,description,sensitivity,agent_access,review_status,business_term,allowed_values,extra) '
                      'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', (vid, entity_key, col, pos, *cols, J(av) if av is not None else None, J(extra)))
        _audit(c, user, vid, 'update' if old else 'create', 'field', f'{entity_key}.{col}', O.field_dict(old) if old else None, data)
        return data


def delete_field(user, entity_key, col):
    with conn() as c:
        vid = draft(c)
        old = c.execute('DELETE FROM field WHERE version_id=%s AND entity_key=%s AND col=%s RETURNING *', (vid, entity_key, col)).fetchone()
        if not old:
            raise EditError('field not found')
        _audit(c, user, vid, 'delete', 'field', f'{entity_key}.{col}', O.field_dict(old), None)


upsert_attribute, delete_attribute = upsert_field, delete_field          # the same table: kept under the old names for callers and tests


# ---------- relationships
def upsert_relationship(user, rid, data):
    _need(data, REL_REQUIRED, 'relationship')
    if data['status'] not in REL_STATUS:
        raise EditError(f'relationship: status must be one of {sorted(REL_STATUS)}')
    data = {**data, 'id': rid}
    with conn() as c:
        vid = draft(c)
        names = {r['name'] for r in c.execute('SELECT name FROM entity WHERE version_id=%s', (vid,)).fetchall()}
        for end in ('from', 'to'):
            if not ref_ok(data[end], names):
                raise EditError(f'relationship: unknown entity "{data[end]}" in {end}')
        old = c.execute('SELECT * FROM relationship WHERE version_id=%s AND rid=%s', (vid, rid)).fetchone()
        extra = {k: v for k, v in data.items() if k not in O.REL_COLS and k != 'id'}
        vals = (data['from'], data['to'], data.get('predicate'), data.get('cardinality'), data.get('join'), data.get('status'), J(extra))
        if old:
            c.execute('UPDATE relationship SET from_entity=%s,to_entity=%s,predicate=%s,cardinality=%s,join_cond=%s,status=%s,extra=%s WHERE version_id=%s AND rid=%s', (*vals, vid, rid))
        else:
            pos = c.execute('SELECT coalesce(max(position),-1)+1 AS p FROM relationship WHERE version_id=%s', (vid,)).fetchone()['p']
            c.execute('INSERT INTO relationship (version_id,rid,position,from_entity,to_entity,predicate,cardinality,join_cond,status,extra) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', (vid, rid, pos, *vals))
        _audit(c, user, vid, 'update' if old else 'create', 'relationship', rid, dict(old) if old else None, data)
        return data


def next_relationship_id():
    with conn() as c:
        vid = draft(c)
        ids = [int(r['rid'].split('-')[1]) for r in c.execute('SELECT rid FROM relationship WHERE version_id=%s', (vid,)).fetchall() if r['rid'].startswith('R-')]
        return f'R-{max(ids, default=0) + 1:03d}'


def delete_relationship(user, rid):
    with conn() as c:
        vid = draft(c)
        old = c.execute('DELETE FROM relationship WHERE version_id=%s AND rid=%s RETURNING *', (vid, rid)).fetchone()
        if not old:
            raise EditError('relationship not found')
        _audit(c, user, vid, 'delete', 'relationship', rid, dict(old), None)


# ---------- examples (query patterns)
def upsert_example(user, kid, data):
    _need(data, ['title', 'sql'], 'example')
    if not str(data.get('sql', '')).strip():
        raise EditError('an example needs its SQL')
    if not isinstance(data.get('applies_to', []), list):
        raise EditError('example: applies_to must be a list of entity keys')
    data = {**data, 'id': kid, 'body': data.get('body') or '', 'applies_to': data.get('applies_to', [])}
    data.pop('type', None); data.pop('pinned', None)
    with conn() as c:
        vid = draft(c)
        keys = {r['key'] for r in c.execute('SELECT key FROM entity WHERE version_id=%s', (vid,)).fetchall()}
        bad = [a for a in data['applies_to'] if a not in keys]
        if bad:
            raise EditError(f'example: unknown entity key(s) in applies_to: {bad}')
        old = c.execute('SELECT * FROM example WHERE version_id=%s AND id=%s', (vid, kid)).fetchone()
        extra = {k: v for k, v in data.items() if k not in O.EX_COLS and k != 'id'}
        vals = (data['title'], data['body'], J(data['applies_to']), J(extra))
        if old:
            c.execute('UPDATE example SET title=%s,body=%s,applies_to=%s,extra=%s WHERE version_id=%s AND id=%s', (*vals, vid, kid))
        else:
            pos = c.execute('SELECT coalesce(max(position),-1)+1 AS p FROM example WHERE version_id=%s', (vid,)).fetchone()['p']
            c.execute('INSERT INTO example (version_id,id,position,title,body,applies_to,extra) VALUES (%s,%s,%s,%s,%s,%s,%s)', (vid, kid, pos, *vals))
        _audit(c, user, vid, 'update' if old else 'create', 'example', kid, dict(old) if old else None, data)
        return data


def delete_example(user, kid):
    with conn() as c:
        vid = draft(c)
        old = c.execute('DELETE FROM example WHERE version_id=%s AND id=%s RETURNING *', (vid, kid)).fetchone()
        if not old:
            raise EditError('example not found')
        _audit(c, user, vid, 'delete', 'example', kid, dict(old), None)


# ---------- validation before publish
def sql_warnings(onto):
    """Filters and checks whose SQL does not run on the connected database. Advice only: the model may be ahead of the database, or written before
    any database is connected, so this never blocks publishing."""
    from .synth import ddl
    errs, jobs = [], []
    for k, e in onto['entities'].items():
        for f in e.get('filters', []):
            jobs.append((f"filter '{f['name']}' on {e['name']}", f"EXPLAIN SELECT 1 FROM {ddl.qualified(e)} WHERE {f['filter']}"))
        for c in e.get('checks', []):
            if c.get('sql'):
                jobs.append((f"check {c['id']} on {e['name']}", f"EXPLAIN {c['sql']}"))
    if jobs:
        try:
            from . import access, datastore
            if datastore.available():
                for label, sql in jobs:
                    try:
                        datastore.console(sql, access.FULL, trusted=True)
                    except datastore.DataError as ex:
                        errs.append(f'{label}: does not run on the connected database: {ex}')
        except Exception:
            pass                                                       # no data database to test against: structure checks only
    return errs


def validate(vid):
    """Problems that must be fixed before a draft can be published (the model's own consistency; nothing depends on a database)."""
    return validate_model(O.assemble(vid))


def validate_model(onto):
    """The same checks on a model dict."""
    errs = []
    ents = onto['entities']
    names = {e['name'] for e in ents.values()}
    for k, e in ents.items():
        try:
            _need(e, ENTITY_REQUIRED, f'entity {k}')
            _check_entity_lists(e)
        except EditError as ex:
            errs.append(str(ex))
        for col, a in e['attributes'].items():
            try:
                _check_field(a)
            except EditError as ex:
                errs.append(f'{k}.{col}: {ex}')
            if a.get('references') and not ref_ok(a['references'], names):
                errs.append(f'{k}.{col}: references unknown entity "{a["references"]}"')
        for code, m in e['calculated'].items():
            try:
                _check_field(m)
            except EditError as ex:
                errs.append(f'{k}.{code} (calculated): {ex}')
    for r in onto['relationships']:
        for end in ('from', 'to'):
            if not ref_ok(r[end], names):
                errs.append(f'relationship {r["id"]}: unknown entity "{r[end]}"')
        if r.get('status') not in REL_STATUS:
            errs.append(f'relationship {r["id"]}: status must be one of {", ".join(O.REL_STATUS)}')
    for ex in onto['examples']:
        bad = [a for a in ex.get('applies_to', []) if a not in ents]
        if bad:
            errs.append(f"example {ex['id']}: applies to unknown entity {bad}")
        if not ex.get('sql'):
            errs.append(f"example {ex['id']}: no SQL")
    if not errs:
        try:
            build_chunks(onto)
        except (KeyError, TypeError) as ex:
            errs.append(f'cannot build search chunks: missing field {ex}')
    return errs
