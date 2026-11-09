"""Read-only access to the data: table browser, row navigation and a SQL console, on the built-in synthetic database or on the active connector.

Safety, in layers: (1) enforce.py parses every console query and decides, column by column, what the caller may read (restricted never, entitlements,
stable-hash masking); the statement that runs is regenerated from the checked tree. (2) The database account is read-only with statement and idle timeouts; on the
built-in database it is one of 8 roles with column grants matching the entitlements. Table and column names from the browser are validated against the model;
values are always bound parameters.
"""
import re
import threading
import time
from decimal import Decimal

from . import access, connectors, drivers, enforce, settings
from .synth import ddl

MISSING = re.compile(r"(?i)no such table|doesn'?t exist|does not exist|unknown_table|unknown table|catalog error.*(table|schema)|not found")
_lock = threading.Lock()
MAX_PAGE = 200
_builtin = []
_active = {'at': 0.0, 'row': None}


class DataError(ValueError):
    pass


def active_connector():
    """The active connector row, or None for the built-in database (cached for a few seconds; changes call reset_pools)."""
    if time.monotonic() - _active['at'] > 3:
        _active.update(at=time.monotonic(), row=connectors.active())
    return _active['row']


def driver():
    """The driver of whatever the data lives in: the active connector, or the built-in demo database."""
    row = active_connector()
    if row:
        try:
            return connectors.driver(row)
        except (connectors.ConnectorError, drivers.DriverError) as e:
            raise DataError(str(e)) from e
    with _lock:
        if not _builtin:
            _builtin.append(drivers.BuiltinDriver())
        return _builtin[0]


def run(sql, params=None, max_rows=None, ent=None):
    """Run one statement on the data: (columns, rows as dicts). Errors become DataError (privilege problems get their own message)."""
    try:
        return driver().run(sql, params, max_rows, ent)
    except drivers.DriverError as ex:
        if ex.privilege:
            raise DataError('permission denied: the query touches columns your entitlements do not cover (confidential, personal or restricted data). '
                            'List the columns you need instead of SELECT *.') from ex
        msg = str(ex)
        row = active_connector()
        if row and MISSING.search(msg):                       # a missing table on a database that is clearly not the modelled one: say that, once
            hint = connectors.mismatch_hint(row)
            if hint:
                msg = f'{msg}. {hint}'
        raise DataError(msg) from ex


def connection(ent=None):
    """A connection for code that runs many statements (the Model check). PostgreSQL: the pooled psycopg connection; others: an adapter with the same execute()."""
    drv = driver()
    if drv.dialect == 'postgres':
        return drv.pool(ent).connection()
    return _AdapterCtx(drv)


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class _AdapterCtx:
    """psycopg-like execute() over any driver: the model's SQL is in PostgreSQL syntax and is translated; named %(x)s parameters become positional."""

    def __init__(self, drv):
        self.drv = drv

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if isinstance(params, dict):
            names = re.findall(r'%\((\w+)\)s', sql)
            sql = re.sub(r'%\(\w+\)s', '%s', sql)
            params = [params[n] for n in names]
        sql = self.drv.to_dialect(sql.replace('%s', '__P__')).replace('__P__', '%s') if self.drv.dialect != 'postgres' else sql
        try:
            _cols, rows = self.drv.run(sql, params or None)
        except drivers.DriverError as ex:
            raise DataError(str(ex)) from ex
        return _Cur(rows)

    def rollback(self):
        pass


def reset_pools():
    """The database was rebuilt, or the active connector changed: drop every pooled connection."""
    _active['at'] = 0.0
    with _lock:
        for d in _builtin:
            d.close()
        _builtin.clear()
    drivers.close_all()


def available():
    from . import databuild
    if not active_connector() and databuild.last_ok() is None:       # no connector and no built demo database: if it does not even exist there is nothing to reach (and no point retrying)
        from .db import conn
        from .synth.load import DB_NAME
        with conn() as c:
            if not c.execute('SELECT 1 FROM pg_database WHERE datname=%s', (DB_NAME,)).fetchone():
                return False
    try:
        run('SELECT 1', ent=access.FULL)
        return True
    except Exception:
        return False


class Catalog:
    """What the model says the database looks like: tables, columns, foreign keys. Built from the published ontology."""

    def __init__(self, onto):
        self.onto = onto
        self.ents = onto['entities']
        self.by_table = {e['semantic_table']: k for k, e in self.ents.items()}
        self.fks = ddl.foreign_keys(onto)
        self.fk_of = {(k, c): t for k, c, t in self.fks}
        self.children = {}
        for k, c, t in self.fks:
            self.children.setdefault(t, []).append((k, c))
        self.schema = enforce.Schema(self.ents)

    def entity(self, semantic_table):
        k = self.by_table.get(semantic_table)
        if not k:
            raise DataError(f'unknown table {semantic_table}')
        return k, self.ents[k]

    def columns(self, key, ent):
        e = self.ents[key]
        out = []
        for c, a in e['attributes'].items():
            d = enforce.decide(a, ent)
            fk = self.fk_of.get((key, c))
            out.append({'name': c, 'type': a['type'], 'role': a['role'], 'business_name': a['business_name'], 'description': a.get('description', ''),
                        'sensitivity': a['sensitivity'], 'hidden': d == 'deny', 'masked': d == 'hash',
                        'fk': {'table': self.ents[fk]['semantic_table'], 'entity': fk, 'column': ddl.pk_col(self.ents[fk])} if fk else None})
        return out


def _json(v):
    if isinstance(v, Decimal):
        return float(v)
    return v


def _clean(row):
    return {k: _json(v) for k, v in row.items()}


def _op(drv, op, col):
    """Filter fragment for the table browser, in this database's syntax (value bound as %s)."""
    c = drv.q(col)
    return {'eq': f'{drv.cast_text(c)} = %s', 'contains': f'LOWER({drv.cast_text(c)}) LIKE LOWER(%s)', 'gt': f'{c} > %s', 'lt': f'{c} < %s',
            'null': f'{c} IS NULL', 'notnull': f'{c} IS NOT NULL'}.get(op)


COUNT_CAP = 100001


def overview(cat):
    """Every table with its row count: exact on the built-in database, exact up to a cap on a connector (big tables are not scanned)."""
    ents = cat.ents
    drv = driver()
    ext = active_connector() is not None
    counts, capped = {}, set()
    for k, e in ents.items():                                     # one small query per table: portable, and a missing table names itself
        q = drv.qualified(e)
        try:
            _c, rows = run(drv.count_capped(q, COUNT_CAP) if ext else f'SELECT count(*) AS n FROM {q}', ent=access.FULL)
        except DataError as ex:
            raise DataError(f'cannot count {e["semantic_table"]}: {ex}') from ex
        counts[k] = rows[0]['n']
        if ext and counts[k] >= COUNT_CAP:
            capped.add(k)
    return [{'key': k, 'table': e['semantic_table'], 'name': e['name'], 'kind': e['kind'], 'domain': e['domain'], 'rows': counts.get(k, 0), 'capped': k in capped,
             'columns': len(e['attributes'])} for k, e in ents.items()]


def _select_list(drv, cols):
    """SELECT list for the columns a caller may see: masked ones come out as their stable hash."""
    from sqlglot import exp
    out = []
    for c in cols:
        if c['hidden']:
            continue
        q = drv.q(c['name'])
        out.append(f"{drv.hash_expr(exp.column(c['name'], quoted=True)).sql(dialect=drv.dialect)} AS {q}" if c['masked'] else q)
    return ', '.join(out)


def page(cat, semantic_table, ent, page_no=1, size=50, sort=None, desc=False, filters=None):
    key, e = cat.entity(semantic_table)
    ent = access.normalize(ent)
    drv = driver()
    cols = cat.columns(key, ent)
    visible = [c['name'] for c in cols if not c['hidden']]
    masked = {c['name'] for c in cols if c['masked']}
    size = min(max(size, 1), MAX_PAGE)
    where, params = [], []
    for f in filters or []:
        col, op = f.get('col'), f.get('op', 'contains')
        frag = _op(drv, op, col) if col in visible else None
        if frag is None:
            raise DataError(f'bad filter {col} {op}')
        if col in masked:
            raise DataError(f'{col} is masked: it cannot be filtered (its values are shown as a stable hash)')
        if '%s' in frag:
            v = str(f.get('value', ''))
            params.append(f'%{v}%' if op == 'contains' else v)
        where.append(frag)
    wsql = (' WHERE ' + ' AND '.join(where)) if where else ''
    q = drv.qualified(e)
    order = f" ORDER BY {drv.q(sort)} {'DESC' if desc else 'ASC'}" if sort in visible else f" ORDER BY {drv.q(ddl.pk_col(e))}"
    _c, tot = run(f'SELECT count(*) AS n FROM {q}{wsql}', params, ent=ent)
    _c, rows = run(f'SELECT {_select_list(drv, cols)} FROM {q}{wsql}{order} LIMIT {size} OFFSET {(max(page_no, 1) - 1) * size}', params, ent=ent)
    return {'table': semantic_table, 'entity': key, 'name': e['name'], 'definition': e.get('definition', ''), 'columns': cols, 'rows': [_clean(r) for r in rows],
            'total': tot[0]['n'], 'page': page_no, 'size': size, 'pk': ddl.pk_cols(e)}


def row_detail(cat, semantic_table, pk_value, ent):
    """One row plus, for every table that points at it, how many rows do (click through to those rows)."""
    key, e = cat.entity(semantic_table)
    ent = access.normalize(ent)
    drv = driver()
    cols = cat.columns(key, ent)
    pk = ddl.pk_col(e)
    _c, found = run(f"SELECT {_select_list(drv, cols)} FROM {drv.qualified(e)} WHERE {drv.q(pk)} = %s", [pk_value], ent=ent)
    if not found:
        raise DataError('row not found')
    children = []
    for ck, cc in cat.children.get(key, []):
        ce = cat.ents[ck]
        _c, r = run(f"SELECT count(*) AS n FROM {drv.qualified(ce)} WHERE {drv.q(cc)} = %s", [pk_value], ent=ent)
        if r[0]['n']:
            children.append({'table': ce['semantic_table'], 'entity': ck, 'name': ce['name'], 'column': cc, 'rows': r[0]['n']})
    return {'table': semantic_table, 'entity': key, 'name': e['name'], 'columns': cols, 'row': _clean(found[0]), 'referenced_by': children}


_cats = {}


def current_catalog():
    """The model catalog for the data in use: the active connector is described by the published model; the built-in database by the version it was built from."""
    from . import databuild
    from .ontology import assemble, published_id
    if not active_connector():
        cat = databuild.catalog()[0]
        if cat is not None:
            return cat
    vid = published_id()
    if not vid:
        raise DataError('there is no published ontology yet')
    # a connector, or a built-in database without a build record: the published model describes it
    if ('pub', vid) not in _cats:
        _cats.clear()
        _cats[('pub', vid)] = Catalog(assemble(vid))
    return _cats[('pub', vid)]


def console(sql, ent, cat=None, trusted=False, max_rows=None):
    """Run one read-only statement. Returns columns, up to the row limit (or max_rows, if lower) and whether the result was cut.
    The statement is checked and regenerated by enforce.check; trusted=True (the app's own publish-time checks) only enforces read-only."""
    ent = access.normalize(ent)
    drv = driver()
    row = active_connector()
    most = settings.get('CONSOLE_MAX_ROWS')
    cap = min(most, int({**connectors.DEFAULT_OPTIONS, **(row['options'] or {})}['row_limit'])) if row else most
    cap = min(cap, max_rows) if max_rows else cap
    s = sql.strip().rstrip(';').strip()
    if not s:
        raise DataError('empty query')
    hashed = []
    if trusted:
        s = drv.to_dialect(s)
        if ';' in re.sub(r"'(?:[^']|'')*'", '', s):
            raise DataError('one statement at a time')
        if not re.match(r'(?is)^\s*(select|with|explain|table|values|show)\b', s):
            raise DataError('read-only console: start with SELECT, WITH, EXPLAIN, TABLE or VALUES')
    else:
        cat = cat or current_catalog()
        if cat is None:
            raise DataError('no database to query yet')
        try:
            s, info = enforce.check(s, cat.schema, ent, limit=cap + 1, drv=drv)
            hashed = info['hashed']
        except enforce.Refused as ex:
            raise DataError(str(ex)) from ex
    names, rows = run(s, None, cap + 1, ent)
    return {'columns': names, 'rows': [_clean(r) for r in rows[:cap]], 'truncated': len(rows) > cap, 'masked_columns': hashed}
