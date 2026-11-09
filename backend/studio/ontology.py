"""Read and write the model in Postgres. The assembled shape has four concepts:

  entities       {key: {name, domain, kind, definition, synonyms, filters, checks, attributes{col: field}, calculated{code: field}, custom?, ...}}
  relationships  [{id, from, to, predicate, cardinality, join, status: confirmed|optional|unreliable, ...}]
  examples       [{id, title, body, applies_to[], sql, params?, sample_sql?, ...}]   (query patterns: the only free-form content the model holds; rules, terms and processes live in the Library)
  ontology / access_policy   (version metadata)

A field is an attribute (a column of the entity's table) or a calculated field (role 'calculated': no column; an optional SQL that tells agents how
it is computed). Anything a company wants to record beyond these properties lives in 'custom' ({name: text}) on any item.
"""
from .db import conn, J

ENTITY_COLS = ('name', 'domain', 'kind', 'definition', 'semantic_table', 'primary_key', 'business_key', 'system_of_record')
ENTITY_LISTS = ('synonyms', 'filters', 'checks')
ENTITY_DERIVED = ('id', 'attributes', 'calculated', 'relationships_out', 'relationships_in', 'stats', 'subtypes')
FIELD_COLS = ('role', 'type', 'business_name', 'description', 'sensitivity', 'agent_access', 'review_status', 'business_term')
REL_COLS = {'from': 'from_entity', 'to': 'to_entity', 'predicate': 'predicate', 'cardinality': 'cardinality', 'join': 'join_cond', 'status': 'status'}
EX_COLS = ('title', 'body', 'applies_to')
META_KEYS = ('ontology', 'access_policy')
REL_STATUS = ('confirmed', 'optional', 'unreliable')


# ------------------------------------------------------------------ rows <-> dicts
def entity_row(key, e):
    extra = {k: v for k, v in e.items() if k not in ENTITY_COLS + ENTITY_LISTS + ENTITY_DERIVED}
    return [e.get(c) for c in ENTITY_COLS], [e.get(l, []) for l in ENTITY_LISTS], extra


def field_row(f):
    extra = {k: v for k, v in f.items() if k not in FIELD_COLS + ('allowed_values',)}
    return [f.get(c) for c in FIELD_COLS], f.get('allowed_values'), extra


def field_dict(r):
    f = {c: r[c] for c in FIELD_COLS if r[c] is not None}
    if r['allowed_values'] is not None:
        f['allowed_values'] = r['allowed_values']
    f.update(r['extra'] or {})
    return f


def save_model(c, vid, m):
    """Write a model dict (the assembled shape) as the rows of version vid."""
    with c.cursor() as cur:
        for pos, (key, e) in enumerate(m['entities'].items()):
            cols, lists, extra = entity_row(key, e)
            cur.execute('INSERT INTO entity (version_id,key,position,name,domain,kind,definition,semantic_table,primary_key,business_key,system_of_record,synonyms,filters,checks,extra) '
                        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', (vid, key, pos, *cols, *[J(x) for x in lists], J(extra)))
        rows = []
        for key, e in m['entities'].items():
            fields = list(e['attributes'].items()) + list(e.get('calculated', {}).items())
            for pos, (col, f) in enumerate(fields):
                cols, av, extra = field_row(f)
                rows.append((vid, key, col, pos, *cols, J(av) if av is not None else None, J(extra)))
        cur.executemany('INSERT INTO field (version_id,entity_key,col,position,role,type,business_name,description,sensitivity,agent_access,review_status,business_term,allowed_values,extra) '
                        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', rows)
        for pos, r in enumerate(m['relationships']):
            extra = {k: v for k, v in r.items() if k not in REL_COLS and k != 'id'}
            cur.execute('INSERT INTO relationship (version_id,rid,position,from_entity,to_entity,predicate,cardinality,join_cond,status,extra) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        (vid, r['id'], pos, r['from'], r['to'], r.get('predicate'), r.get('cardinality'), r.get('join'), r.get('status'), J(extra)))
        for pos, k in enumerate(m.get('examples', [])):
            extra = {x: v for x, v in k.items() if x not in EX_COLS and x not in ('id', 'type', 'pinned')}
            cur.execute('INSERT INTO example (version_id,id,position,title,body,applies_to,extra) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                        (vid, k['id'], pos, k.get('title'), k.get('body'), J(k.get('applies_to', [])), J(extra)))
        for key in META_KEYS:
            if m.get(key) is not None:
                cur.execute('INSERT INTO meta (version_id,key,data) VALUES (%s,%s,%s)', (vid, key, J(m[key])))


def assemble(vid):
    """The full model of one version, in the shape described at the top of this file."""
    with conn() as c:
        ents = c.execute('SELECT * FROM entity WHERE version_id=%s ORDER BY position', (vid,)).fetchall()
        fields = c.execute('SELECT * FROM field WHERE version_id=%s ORDER BY entity_key, position', (vid,)).fetchall()
        rels = c.execute('SELECT * FROM relationship WHERE version_id=%s ORDER BY position', (vid,)).fetchall()
        ex = c.execute('SELECT * FROM example WHERE version_id=%s ORDER BY position', (vid,)).fetchall()
        meta = {r['key']: r['data'] for r in c.execute('SELECT key,data FROM meta WHERE version_id=%s', (vid,)).fetchall()}
    entities = {}
    for r in ents:
        e = {'id': r['key']}
        e.update({c_: r[c_] for c_ in ENTITY_COLS if r[c_] is not None})
        e.update(r['extra'] or {})
        for l in ENTITY_LISTS:
            e[l] = r[l] or []
        e['attributes'], e['calculated'] = {}, {}
        entities[r['key']] = e
    for r in fields:
        (entities[r['entity_key']]['calculated'] if r['role'] == 'calculated' else entities[r['entity_key']]['attributes'])[r['col']] = field_dict(r)
    name_key = {e['name']: k for k, e in entities.items()}
    for k, e in entities.items():                                  # the subtypes of an entity are the entities that say they are one of its kinds
        parent = name_key.get(e.get('subtype_of')) or (e.get('subtype_of') if e.get('subtype_of') in entities else None)
        if parent:
            entities[parent].setdefault('subtypes', []).append(e['name'])
    relationships = []
    for r in rels:
        d = {'id': r['rid'], 'from': r['from_entity'], 'to': r['to_entity']}
        d.update({k: r[col] for k, col in REL_COLS.items() if k not in ('from', 'to') and r[col] is not None})
        d.update(r['extra'] or {})
        relationships.append(d)
        src, dst = entities.get(name_key.get(d['from'])), entities.get(name_key.get(d['to']))
        if src is not None:
            src.setdefault('relationships_out', []).append({k: d[k] for k in ('id', 'predicate', 'to', 'cardinality', 'join', 'status', 'status_detail', 'note') if k in d})
        if dst is not None and d['from'] != d['to']:
            dst.setdefault('relationships_in', []).append({k: d[k] for k in ('id', 'from', 'predicate', 'join', 'status') if k in d})
    examples = []
    for r in ex:
        d = {'id': r['id'], 'title': r['title'], 'body': r['body'], 'applies_to': r['applies_to'] or []}
        d.update(r['extra'] or {})
        examples.append(d)
    ontology = dict(meta.get('ontology', {}))
    counts = {'entities': len(entities), 'attributes': sum(len(e['attributes']) for e in entities.values()), 'calculated': sum(len(e['calculated']) for e in entities.values()),
              'relationships': len(relationships), 'examples': len(examples)}
    ontology['counts'] = counts
    return {'ontology': ontology, 'access_policy': meta.get('access_policy', {}), 'entities': entities, 'relationships': relationships, 'examples': examples}


# ------------------------------------------------------------------ versions
def import_model(model, label, note=None, publish=True):
    with conn() as c:
        if publish:
            c.execute("UPDATE ontology_version SET status='archived' WHERE status='published'")
        vid = c.execute("INSERT INTO ontology_version (label,status,note,published_at) VALUES (%s,%s,%s, CASE WHEN %s THEN now() END) RETURNING id",
                        (label, 'published' if publish else 'draft', note, publish)).fetchone()['id']
        save_model(c, vid, model)
    return vid


def clone_version(c, src, label, note=None):
    vid = c.execute("INSERT INTO ontology_version (label,status,note) VALUES (%s,'draft',%s) RETURNING id", (label, note)).fetchone()['id']
    c.execute('INSERT INTO entity SELECT %s,key,position,name,domain,kind,definition,semantic_table,primary_key,business_key,system_of_record,synonyms,filters,checks,extra FROM entity WHERE version_id=%s', (vid, src))
    c.execute('INSERT INTO field SELECT %s,entity_key,col,position,role,type,business_name,description,sensitivity,agent_access,review_status,allowed_values,business_term,extra FROM field WHERE version_id=%s', (vid, src))
    c.execute('INSERT INTO relationship SELECT %s,rid,position,from_entity,to_entity,predicate,cardinality,join_cond,status,extra FROM relationship WHERE version_id=%s', (vid, src))
    c.execute('INSERT INTO example SELECT %s,id,position,title,body,applies_to,extra FROM example WHERE version_id=%s', (vid, src))
    c.execute('INSERT INTO meta SELECT %s,key,data FROM meta WHERE version_id=%s', (vid, src))
    return vid


def published_id(c=None):
    def q(c):
        r = c.execute("SELECT id FROM ontology_version WHERE status='published'").fetchone()
        return r['id'] if r else None
    if c is not None:
        return q(c)
    with conn() as c2:
        return q(c2)


def draft_id():
    with conn() as c:
        r = c.execute("SELECT id FROM ontology_version WHERE status='draft'").fetchone()
        return r['id'] if r else None
