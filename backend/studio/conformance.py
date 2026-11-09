"""Does the model hold on the data? Runs the published model against the connected database (or the demo database).

Groups: structure (the model's tables and columns exist and have rows), relationships (each join: rows linked, orphans), data-quality rules (the SQL
of each entity check counts violations) and examples (each query example runs on sampled parameters). Every check carries the SQL that produced
it. Results are counts and statuses, never values. Nothing here knows a particular model."""
import re
from datetime import date

from . import access, datastore, enforce, modelview
from .synth import ddl

OK, FAIL, WARN, INFO = 'pass', 'fail', 'warn', 'info'


def _soft():
    """On a database that is not PostgreSQL, a statement that does not run is most likely a translation limit of the model's PostgreSQL SQL: a warning, not a failure."""
    return datastore.driver().dialect != 'postgres'


# SQL written in the model (checks, measure rules, examples) is code a modeller wrote: it goes through the same checker as any query (modelled tables only,
# no system functions, never a restricted column), and the page that shows the results to every user shows counts, never values.
GUARD = access.normalize({'confidential': True, 'personal': True, 'restricted': False, 'data': True})


def _model_sql(cat, sql):
    drv = datastore.driver()
    s = drv.to_dialect(sql) if drv.dialect != 'postgres' else sql
    try:
        checked, _info = enforce.check(s, cat.schema, GUARD, drv=drv)
    except enforce.Refused as ex:
        raise datastore.DataError(f'refused by the access checker: {ex}') from ex
    return datastore.run(checked, None, None, access.FULL)[1]


def _err(ex):
    """One line, with quoted literals blanked: a database error can echo a value from a row."""
    line = str(ex).splitlines()[0] if str(ex) else type(ex).__name__
    return re.sub(r"(['\"])(?:(?!\1).)*\1", r"\1…\1", line)[:300]


def _lit(v):
    return 'NULL' if v is None else "'" + str(v).replace("'", "''") + "'"


def _check(group, cid, title, status, detail='', sql=None, rows=None):
    return {'group': group, 'id': cid, 'title': title, 'status': status, 'detail': detail, 'sql': sql, 'rows': rows}


def _one(c, sql, params=None):
    return c.execute(sql, params or ()).fetchone()


def run_all(cat, build_findings=None):
    checks = []
    onto = cat.onto
    with datastore.connection(access.FULL) as c:
        for group in (structure, relationships, entity_checks, examples):
            try:
                checks += group(c, cat)
            except datastore.DataError as ex:                          # a statement this database kind cannot run: say so, keep the other groups
                checks.append(_check(group.__name__.replace('_', ' ').capitalize(), group.__name__, f'{group.__name__.replace("_", " ")} checks could not run on this database', WARN,
                                     f'{str(ex)[:300]} (the model keeps its SQL in PostgreSQL syntax and translates it; a construct may not translate)'))
    for f in build_findings or []:
        checks.append(_check('Model findings', 'F', f, WARN, 'The model itself is inconsistent here; the database loads it with a type-based value instead.'))
    drv = datastore.driver()
    if drv.dialect != 'postgres':                                     # the model's SQL is PostgreSQL; show each check's SQL as it can be run on this database
        from .tools import to_dialect
        for x in checks:
            if x.get('sql'):
                x['sql'] = to_dialect(drv, x['sql'])
    summary = {s: sum(1 for x in checks if x['status'] == s) for s in (OK, FAIL, WARN, INFO)}
    return {'summary': summary, 'checks': checks}


# ------------------------------------------------------------------ structure
def structure(c, cat):
    out = []
    ents = cat.ents
    drv = datastore.driver()
    if drv.dialect == 'postgres':
        # pg_catalog, not information_schema: the latter hides columns the read-only role may not select (restricted ones)
        have = {(r['s'], r['t'], r['c']) for r in c.execute(
            "SELECT n.nspname AS s, cl.relname AS t, a.attname AS c FROM pg_attribute a JOIN pg_class cl ON cl.oid = a.attrelid JOIN pg_namespace n ON n.oid = cl.relnamespace "
            "WHERE a.attnum > 0 AND NOT a.attisdropped AND cl.relkind = 'r' AND n.nspname NOT IN ('pg_catalog', 'information_schema')")}
    else:
        try:
            have = {(t['schema'], t['name'], col['name']) for t in drv.introspect(sorted({ddl.schema_of(e) for e in ents.values()}))['tables'] for col in t['columns']}
        except Exception as ex:
            return [_check('Structure', 'S1', 'The structure of the database can be read', FAIL, str(ex)[:300])]
    missing = [f'{ddl.table_of(e)}.{col}' for e in ents.values() for col in e['attributes'] if (ddl.schema_of(e), ddl.table_of(e), col) not in have]
    out.append(_check('Structure', 'S1', f'All {sum(len(e["attributes"]) for e in ents.values())} model columns exist in {len(ents)} tables',
                      FAIL if missing else OK, f'missing: {missing[:10]}' if missing else ''))
    empty = []
    for k, e in ents.items():
        if _one(c, f'SELECT count(*) AS n FROM {ddl.qualified(e)}')['n'] == 0:
            empty.append(ddl.table_of(e))
    out.append(_check('Structure', 'S2', 'Every table has data', WARN if empty else OK, f'empty: {empty}' if empty else ''))
    if drv.dialect == 'postgres':
        n = _one(c, "SELECT count(*) AS n FROM pg_constraint WHERE contype='f'")['n']
        out.append(_check('Structure', 'S3', f'Foreign keys enforced by the database ({len(cat.fks)} expected)', OK if n == len(cat.fks) else FAIL, f'{n} constraints'))
    else:
        out.append(_check('Structure', 'S3', f'Foreign keys ({len(cat.fks)} in the model)', INFO, f'not compared: {drv.label} does not declare or expose them the way PostgreSQL does'))
    return out


# ------------------------------------------------------------------ relationships
JOIN = re.compile(r'^\s*(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)\s*$')


def relationships(c, cat):
    out = []
    by_tab = {ddl.table_of(e): (k, e) for k, e in cat.ents.items()}
    for r in cat.onto['relationships']:
        m = JOIN.match(r['join'])
        label = f"{r['id']} {r['from']} -[{r['predicate']}]-> {r['to']}"
        if not m or m.group(1) not in by_tab or m.group(3) not in by_tab:
            out.append(_check('Relationships', r['id'], label, INFO, f"join '{r['join']}' refers to a legacy or unmodelled table: not checkable on this schema"))
            continue
        (ta, ca, tb, cb) = m.groups()
        ka, ea = by_tab[ta]
        kb, eb = by_tab[tb]
        if ca not in ea['attributes'] or cb not in eb['attributes']:
            out.append(_check('Relationships', r['id'], label, FAIL, f"join column missing: {ta}.{ca} or {tb}.{cb}"))
            continue
        # the side holding the foreign key is the 'many' side: the one whose column is not the primary key
        child, ccol, parent, pcol = (ea, ca, eb, cb) if ca not in ddl.pk_cols(ea) or cb in ddl.pk_cols(eb) else (eb, cb, ea, ca)
        sql = (f"SELECT count(*) AS total, count(x.{ddl.q(ccol)}) AS linked, "
               f"coalesce(sum(CASE WHEN x.{ddl.q(ccol)} IS NOT NULL AND y.k IS NULL THEN 1 ELSE 0 END), 0) AS orphans "
               f"FROM {ddl.qualified(child)} x LEFT JOIN (SELECT DISTINCT {ddl.q(pcol)} AS k FROM {ddl.qualified(parent)}) y ON y.k = x.{ddl.q(ccol)}")
        row = _one(c, sql)
        row = {k: int(v) for k, v in row.items()}
        st, detail = r['status'], f"{row['linked']}/{row['total']} rows linked, {row['orphans']} orphans · status {r['status']} · {r['cardinality']}"
        if row['orphans']:
            status = FAIL
        elif st == 'confirmed' and row['total'] and not row['linked']:
            status = WARN
            detail += ' · confirmed link never used in the data'
        elif st in ('optional', 'unreliable'):
            status = INFO
        else:
            status = OK
        out.append(_check('Relationships', r['id'], label, status, detail, sql))
    return out


# ------------------------------------------------------------------ checks on entities (data quality rules)
def entity_checks(c, cat):
    """Every check attached to an entity: its SQL returns the number of violations, which must be 0."""
    G = 'Data quality rules'
    out = []
    for k, e in cat.ents.items():
        for chk in e.get('checks', []):
            if not chk.get('sql'):
                out.append(_check(G, f"{k}.{chk['id']}", f"{e['name']}: {chk['rule']}", INFO, 'no SQL attached to this check: nothing to run'))
                continue
            try:
                n = int(_model_sql(cat, chk['sql'])[0]['n'])
            except Exception as ex:
                out.append(_check(G, f"{k}.{chk['id']}", f"{e['name']}: {chk['rule']}", WARN if _soft() else FAIL, f"the check's SQL does not run: {_err(ex)}", chk['sql']))
                continue
            status = FAIL if n else (WARN if chk.get('attention') else OK)
            out.append(_check(G, f"{k}.{chk['id']}", f"{e['name']}: {chk['rule']}", status, (f'{n} violations. ' if n else '') + (chk.get('note') or ''), chk['sql']))
    return out


# ------------------------------------------------------------------ query examples
PARAM = re.compile(r'(?<![:\w]):([a-z_]+)')
TRIES = 60


def _sample_ids(c, cat, entity_key, n=TRIES):
    e = cat.ents[entity_key]
    return [r['v'] for r in c.execute(f"SELECT {ddl.q(ddl.pk_col(e))}::text AS v FROM {ddl.qualified(e)} ORDER BY random() LIMIT {n}")]


def examples(c, cat):
    """Each example with SQL is run on real rows: bind parameters are sampled from the entity each one refers to (or from the
    example's own sample query) until the statement returns rows. A statement that does not run on the schema is a finding."""
    G = 'Query examples'
    out = []
    for ex in modelview.examples(cat.onto):
        if not ex.get('sql'):
            continue
        names = list(dict.fromkeys(PARAM.findall(ex['sql'])))
        tries, from_keys = [], False
        if ex.get('sample_sql'):
            try:
                tries = _model_sql(cat, ex['sample_sql'])[:TRIES]
            except Exception as e_:
                out.append(_check(G, ex['id'], ex['title'], WARN if _soft() else FAIL, f"the example's sample query does not run: {_err(e_)}", ex['sample_sql']))
                continue
        elif names:
            main = next((n for n in names if n in ex.get('params', {})), None)
            if main is None:
                out.append(_check(G, ex['id'], ex['title'], WARN, f"parameters {names} are not mapped to an entity (params): the check cannot pick sample rows", ex['sql']))
                continue
            tries = [{main: v} for v in _sample_ids(c, cat, ex['params'][main])]
            pk = cat.ents[ex['params'][main]]['attributes'].get(ddl.pk_col(cat.ents[ex['params'][main]]), {})
            from_keys = pk.get('sensitivity', 'internal') == 'internal' and pk.get('agent_access', 'full') == 'full'      # plain identifiers may be shown
        else:
            tries = [{}]
        result, used, err = None, None, None
        for t in tries:
            try:
                rows = _model_sql(cat, PARAM.sub(lambda m: _lit(t.get(m.group(1))), ex['sql']))
            except Exception as e_:                                    # a statement that does not run on the schema is the finding
                err = _err(e_)
                break
            used = used or t
            if rows:
                result, used = rows, t
                break
        # the SQL shown (and offered in the console) carries sample values only when they are plain identifiers; otherwise the :parameters stay
        shown = PARAM.sub(lambda m: _lit((used or {}).get(m.group(1), '')), ex['sql']) if from_keys and used else ex['sql']
        if err:
            out.append(_check(G, ex['id'], ex['title'], WARN if _soft() else FAIL, f'does not run on the schema: {err}', ex['sql']))
        elif result:
            out.append(_check(G, ex['id'], ex['title'], OK, f'{len(result)} rows for a sampled {"parameter" if not names else ", ".join(names)}', shown, rows=len(result)))
        else:
            out.append(_check(G, ex['id'], ex['title'], WARN, f'runs, but returned no rows for any of {len(tries)} samples: the data does not exercise this example', shown))
    return out
