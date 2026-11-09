"""Derive the physical schema from the semantic model: every entity is a table, every attribute a column.
The ontology stays the single source of truth; the database can be rebuilt from any published version."""
import re

PG_TYPE = {'text': 'text', 'integer': 'integer', 'boolean': 'boolean', 'timestamp': 'timestamp', 'decimal': 'numeric(18,2)',
           'decimal (ratio)': 'numeric(9,4)', 'date': 'date', 'text[]': 'text[]'}


def q(name):
    return '"' + name.replace('"', '""') + '"'


def schema_of(entity):
    return entity['semantic_table'].split('.')[0]


def table_of(entity):
    return entity['semantic_table'].split('.')[1]


def qualified(entity):
    return f'{q(schema_of(entity))}.{q(table_of(entity))}'


def is_restricted(a):
    return a.get('agent_access') == 'none' or a.get('sensitivity') == 'restricted'


def pk_cols(entity):
    """Primary key columns by role (a few reference tables have composite keys)."""
    return [c for c, a in entity['attributes'].items() if a['role'] == 'primary_key'] or ['id']


def pk_col(entity):
    return pk_cols(entity)[0]


def resolve_ref(ref, ents):
    """Entity key a foreign key points to, or None (alternate references and polymorphic ones are not constraints)."""
    by_name = {e['name']: k for k, e in ents.items()}
    short = {e['name'].split(' (')[0]: k for k, e in ents.items()}
    if ref in by_name:
        return by_name[ref]
    return short.get(ref.split(' (')[0].strip())


def foreign_keys(onto):
    """[(entity_key, column, target_entity_key)] for every foreign_key attribute that resolves to one entity."""
    ents = onto['entities']
    out = []
    for k, e in ents.items():
        for c, a in e['attributes'].items():
            if a['role'] == 'foreign_key' and a.get('references'):
                t = resolve_ref(a['references'], ents)
                if t:
                    out.append((k, c, t))
    return out


def build_tables_sql(onto):
    ents = onto['entities']
    stmts = [f'CREATE SCHEMA IF NOT EXISTS {q(s)}' for s in sorted({schema_of(e) for e in ents.values()})]
    for k, e in ents.items():
        cols = []
        for c, a in e['attributes'].items():
            cols.append(f"  {q(c)} {PG_TYPE.get(a['type'], 'text')}" + (' NOT NULL' if c in pk_cols(e) else ''))
        cols.append(f"  PRIMARY KEY ({', '.join(q(c) for c in pk_cols(e))})")
        stmts.append(f'CREATE TABLE {qualified(e)} (\n' + ',\n'.join(cols) + '\n)')
    return stmts


def comments_sql(onto):
    ents = onto['entities']
    lit = lambda s: "'" + str(s).replace("'", "''") + "'"
    stmts = []
    for k, e in ents.items():
        stmts.append(f"COMMENT ON TABLE {qualified(e)} IS {lit(e['name'] + ': ' + (e.get('definition') or ''))}")
        for c, a in e['attributes'].items():
            text = f"{a['business_name']}. {a.get('description', '')} [{a['sensitivity']}, {a['review_status']}]"
            stmts.append(f"COMMENT ON COLUMN {qualified(e)}.{q(c)} IS {lit(text)}")
    return stmts


def index_sql(onto):
    ents = onto['entities']
    return [f"CREATE INDEX {q('ix_' + table_of(ents[k]) + '_' + c)} ON {qualified(ents[k])} ({q(c)})" for k, c, t in foreign_keys(onto)]


def fk_sql(onto):
    ents = onto['entities']
    return [f"ALTER TABLE {qualified(ents[k])} ADD CONSTRAINT {q('fk_' + table_of(ents[k]) + '_' + c)} FOREIGN KEY ({q(c)}) "
            f"REFERENCES {qualified(ents[t])} ({q(pk_col(ents[t]))})" for k, c, t in foreign_keys(onto) if len(pk_cols(ents[t])) == 1]   # a composite key cannot be referenced by one column


def orphan_sql(onto):
    """[(label, sql)]: rows whose foreign key points nowhere. Must all be 0 before constraints are added."""
    ents = onto['entities']
    out = []
    for k, c, t in foreign_keys(onto):
        out.append((f'{table_of(ents[k])}.{c} -> {table_of(ents[t])}',
                    f"SELECT count(*) FROM {qualified(ents[k])} x WHERE x.{q(c)} IS NOT NULL AND NOT EXISTS "
                    f"(SELECT 1 FROM {qualified(ents[t])} y WHERE y.{q(pk_col(ents[t]))} = x.{q(c)})"))
    return out


def allowed(a, ent):
    """May a role with these entitlements read this column (raw, or as a stable hash for masked columns)? See enforce.decide."""
    from ..enforce import decide
    return decide(a, ent) != 'deny'


def grants_sql(onto, role, ent):
    """Column-level SELECT. A role cannot read columns it is not entitled to: SELECT * on such a table is refused by the database."""
    ents = onto['entities']
    stmts = []
    for s in sorted({schema_of(e) for e in ents.values()}):
        stmts.append(f'GRANT USAGE ON SCHEMA {q(s)} TO {role}')
    for k, e in ents.items():
        cols = [c for c, a in e['attributes'].items() if allowed(a, ent)]
        if len(cols) == len(e['attributes']):
            stmts.append(f'GRANT SELECT ON {qualified(e)} TO {role}')
        elif cols:
            stmts.append(f"GRANT SELECT ({', '.join(q(c) for c in cols)}) ON {qualified(e)} TO {role}")
    return stmts
