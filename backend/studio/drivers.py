"""One class per database kind. Everything that differs between databases lives here: connecting read-only, running a statement, reading the structure,
quoting, the SQL dialect (sqlglot name), the stable-hash expression for masked columns and the functions that must never be callable.

The rest of the app (connectors, datastore, enforce, planner, conformance) talks to a Driver and does not know which database it is.
Result rows are always a list of dicts. SQL given to run() uses %s placeholders for values.
"""
import hashlib
import os
import re
import sqlite3
import threading
import time
from decimal import Decimal

import sqlglot
from psycopg_pool import ConnectionPool

from . import config

SYSTEM_SCHEMAS = ('pg_catalog', 'information_schema')
DEFAULT_OPTIONS = {'statement_timeout_ms': 15000, 'row_limit': 500, 'agents_may_read_rows': True}
SSL_MODES = ('disable', 'prefer', 'require', 'verify-ca', 'verify-full')
FILES_DIR = os.path.realpath(os.environ.get('CONNECTOR_FILES_DIR', '/app/data'))           # file databases (SQLite, DuckDB) must live under here
BASE_FORBIDDEN = r'(load_extension|readfile|writefile|lo_|dblink|set_config|current_setting|getsetting|query_to_|database_to_|schema_to_|table_to_|cursor_to_|txid_|version|current_user|session_user|' \
                 r'current_database|current_schema|currentuser|currentdatabase|hostname|inet_|has_\w+_privilege|obj_description|col_description|format_type|to_regclass|copy|pg_\w*|' \
                 r'load_file|sleep|benchmark|get_lock|release_lock|sys_exec|sys_eval|user|database|schema|connection_id|' \
                 r'file|url|s3|s3cluster|hdfs|remote|remotesecure|cluster|clusterallreplicas|mysql|postgresql|jdbc|odbc|mongodb|redis|executable|input|dictget\w*|' \
                 r'read_\w+|glob|duckdb_\w+|pragma_\w+|sniff_csv|parquet_\w+|which_secret|getenv)'


def mask_salt():
    return hashlib.sha256(f'{config.SECRET_KEY}:mask'.encode()).hexdigest()[:16]


class DriverError(Exception):
    """A database error, reduced to one clean line. privilege=True when the account lacks a permission."""

    def __init__(self, msg, privilege=False):
        super().__init__(msg)
        self.privilege = privilege


def _one_line(e):
    s = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return re.sub(r'(?i)(password[=:]\s*)\S+', r'\1***', s)[:300]


def _plain(v):
    return float(v) if isinstance(v, Decimal) else v


class Driver:
    kind, label, dialect, default_port = '', '', 'postgres', None
    qchar = '"'
    file_based = False
    forbidden = re.compile('^' + BASE_FORBIDDEN + '$', re.I)
    hash_tpl = ''                                                    # SQL in this dialect: __c__ is the column, __salt__ the salt
    fields = ('host', 'port', 'database', 'user', 'password', 'sslmode', 'schemas')      # what the UI asks for

    def __init__(self, row):
        self.row = row
        self.params = row['params']
        self.options = {**DEFAULT_OPTIONS, **(row.get('options') or {})}

    # ---- connector settings
    @classmethod
    def clean_params(cls, p):
        p = {k: p.get(k) for k in ('host', 'port', 'database', 'user', 'sslmode', 'schemas')}
        if not p['host'] or not p['user']:
            raise ValueError('host and user are required')
        try:
            p['port'] = int(p['port'] or cls.default_port)
        except (TypeError, ValueError) as e:
            raise ValueError('port must be a number') from e
        p['sslmode'] = p['sslmode'] or 'prefer'
        if p['sslmode'] not in SSL_MODES:
            raise ValueError(f'sslmode must be one of {", ".join(SSL_MODES)}')
        p['host'], p['user'] = str(p['host']).strip(), str(p['user']).strip()
        p['database'] = (str(p['database']).strip() if p['database'] else '')
        p['schemas'] = [s.strip() for s in (p.get('schemas') or []) if str(s).strip()]
        return p

    def password(self):
        from .connectors import decrypt
        return decrypt(self.row['secret']) if self.row.get('secret') else None

    # ---- SQL helpers
    def q(self, name):
        return self.qchar + name.replace(self.qchar, self.qchar * 2) + self.qchar

    def qualified(self, entity):
        s, t = entity['semantic_table'].split('.', 1)
        return f'{self.q(s)}.{self.q(t)}'

    def cast_text(self, expr):
        return f'CAST({expr} AS TEXT)'

    def random_fn(self):
        return 'random()'

    def count_capped(self, qualified, cap):
        return f'SELECT count(*) AS n FROM (SELECT 1 FROM {qualified} LIMIT {cap}) s'

    def to_dialect(self, sql):
        """The model keeps its SQL (filters, checks, examples) in PostgreSQL syntax; this is the same statement for this database (best effort: sqlglot plus a few rewrites)."""
        if self.dialect == 'postgres':
            return sql
        try:
            return sqlglot.parse_one(sql, read='postgres').transform(self._rewrite).sql(dialect=self.dialect)
        except sqlglot.errors.SqlglotError:
            return sql

    EPOCH_DIFF = {'mysql': 'TIMESTAMPDIFF(SECOND, {b}, {a})', 'clickhouse': "dateDiff('second', {b}, {a})", 'sqlite': '((julianday({a}) - julianday({b})) * 86400.0)'}

    def _rewrite(self, n):
        from sqlglot import exp
        if isinstance(n, (exp.NullSafeNEQ, exp.NullSafeEQ)):                  # IS [NOT] DISTINCT FROM: not understood everywhere
            a, b = n.this, n.expression
            same = exp.Paren(this=exp.or_(exp.EQ(this=a.copy(), expression=b.copy()), exp.Paren(this=exp.and_(exp.Is(this=a.copy(), expression=exp.Null()), exp.Is(this=b.copy(), expression=exp.Null())))))
            return exp.Not(this=same) if isinstance(n, exp.NullSafeNEQ) else same
        if isinstance(n, exp.Extract) and str(n.this).upper() == 'EPOCH' and isinstance(n.expression, exp.Sub) and self.dialect in self.EPOCH_DIFF:
            tpl = self.EPOCH_DIFF[self.dialect].format(a='__a__', b='__b__')
            t = sqlglot.parse_one(tpl, read=self.dialect)
            return t.transform(lambda m: n.expression.this.copy() if isinstance(m, exp.Column) and m.name == '__a__' else n.expression.expression.copy() if isinstance(m, exp.Column) and m.name == '__b__' else m)
        return n

    def hash_expr(self, col):
        from sqlglot import exp
        tpl = sqlglot.parse_one(self.hash_tpl.replace('__salt__', mask_salt()), read=self.dialect)
        return tpl.transform(lambda n: col.copy() if isinstance(n, exp.Column) and n.name == '__c__' else n)

    # ---- to implement
    def run(self, sql, params=None, max_rows=None, ent=None):
        raise NotImplementedError

    def test(self):
        raise NotImplementedError

    def introspect(self, schemas=None):
        raise NotImplementedError

    def close(self):
        pass


# =============================================================== PostgreSQL
class PostgresDriver(Driver):
    kind, label, dialect, default_port = 'postgres', 'PostgreSQL', 'postgres', 5432
    hash_tpl = "'h_' || substr(encode(sha256(convert_to(CAST(__c__ AS TEXT) || '__salt__', 'UTF8')), 'hex'), 1, 12)"

    def __init__(self, row):
        super().__init__(row)
        self._pool = None
        self._lock = threading.Lock()

    def _conninfo(self):
        import psycopg.conninfo
        p = self.params
        return psycopg.conninfo.make_conninfo(host=p['host'], port=p['port'], dbname=p['database'], user=p['user'], sslmode=p['sslmode'], connect_timeout=10,
                                              password=self.password(), application_name='curltale-data',
                                              options=f"-c default_transaction_read_only=on -c statement_timeout={self.options['statement_timeout_ms']} -c idle_in_transaction_session_timeout=15000")

    def pool(self, ent=None):
        from psycopg.rows import dict_row
        with self._lock:
            if self._pool is None:
                self._pool = ConnectionPool(self._conninfo(), min_size=0, max_size=4, kwargs={'row_factory': dict_row}, check=ConnectionPool.check_connection,
                                            max_lifetime=900, open=True, timeout=10)
            return self._pool

    @classmethod
    def clean_params(cls, p):
        out = super().clean_params(p)
        if not out['database']:
            raise ValueError('database is required')
        return out

    def run(self, sql, params=None, max_rows=None, ent=None):
        import psycopg
        try:
            with self.pool(ent).connection() as c:
                cur = c.cursor()
                cur.execute(sql, params or None)
                if not cur.description:
                    return [], []
                rows = cur.fetchmany(max_rows) if max_rows else cur.fetchall()
                return [d.name for d in cur.description], [{k: _plain(v) for k, v in r.items()} for r in rows]
        except psycopg.errors.InsufficientPrivilege as ex:
            raise DriverError(str(ex).split('\n')[0], privilege=True) from ex
        except psycopg.Error as ex:
            raise DriverError(str(ex).split('\n')[0]) from ex

    def close(self):
        with self._lock:
            if self._pool is not None:
                try:
                    self._pool.close(timeout=2)
                except Exception:
                    pass
                self._pool = None

    def _connect(self, timeout=10):
        import psycopg
        from psycopg.rows import dict_row
        p = self.params
        try:
            return psycopg.connect(host=p['host'], port=p['port'], dbname=p['database'], user=p['user'], password=self.password(), sslmode=p['sslmode'], connect_timeout=timeout,
                                   autocommit=True, row_factory=dict_row, application_name='curltale',
                                   options=f"-c default_transaction_read_only=on -c statement_timeout={self.options['statement_timeout_ms']} -c idle_in_transaction_session_timeout=15000")
        except psycopg.OperationalError as e:
            raise DriverError(_one_line(e)) from e

    def test(self):
        t0 = time.perf_counter()
        with self._connect() as c:
            v = c.execute('SHOW server_version').fetchone()['server_version']
            me = c.execute("SELECT current_user AS u, r.rolsuper, r.rolcreatedb, r.rolcreaterole, has_database_privilege(current_database(),'CREATE') AS can_create "
                           "FROM pg_roles r WHERE r.rolname=current_user").fetchone()
            tables = c.execute("SELECT count(*) AS n FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p','v','m') "
                               "AND n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%%' AND has_table_privilege(c.oid,'SELECT')").fetchone()['n']
            writable = c.execute("SELECT count(DISTINCT table_schema || '.' || table_name) AS n FROM information_schema.role_table_grants "
                                 "WHERE grantee=current_user AND privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE') AND table_schema NOT IN ('pg_catalog','information_schema')").fetchone()['n']
        warn = []
        if me['rolsuper']:
            warn.append('the account is a SUPERUSER: use a read-only account')
        if me['rolcreatedb'] or me['rolcreaterole']:
            warn.append('the account can create databases or roles: use a read-only account')
        if writable:
            warn.append(f'the account can write to {writable} table(s): use a read-only account (queries are forced read-only, but a read-only account is the real protection)')
        if me['can_create']:
            warn.append('the account can create objects in the database')
        return {'version': f'PostgreSQL {v}', 'user': me['u'], 'tables_visible': tables, 'warnings': warn, 'ms': round((time.perf_counter() - t0) * 1000)}

    Q_TABLES = """SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind, c.reltuples::bigint AS est_rows, obj_description(c.oid) AS comment
              FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
              WHERE c.relkind IN ('r','p','v','m') AND NOT c.relispartition AND n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%%'
                AND n.nspname NOT LIKE 'pg_temp%%' AND has_table_privilege(c.oid,'SELECT') AND (%(schemas)s::text[] IS NULL OR n.nspname = ANY(%(schemas)s))
              ORDER BY 1,2"""
    Q_COLUMNS = """SELECT n.nspname AS schema, c.relname AS tbl, a.attname AS name, format_type(a.atttypid,a.atttypmod) AS type, t.typtype AS typtype, t.oid AS typoid,
                      NOT a.attnotnull AS nullable, col_description(c.oid,a.attnum) AS comment
               FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_type t ON t.oid=a.atttypid
               WHERE a.attnum>0 AND NOT a.attisdropped AND c.relkind IN ('r','p','v','m') AND NOT c.relispartition AND n.nspname NOT IN ('pg_catalog','information_schema')
                 AND n.nspname NOT LIKE 'pg_toast%%' AND n.nspname NOT LIKE 'pg_temp%%' AND (%(schemas)s::text[] IS NULL OR n.nspname = ANY(%(schemas)s))
               ORDER BY n.nspname, c.relname, a.attnum"""
    Q_CONS = """SELECT con.contype AS ctype, n.nspname AS schema, c.relname AS tbl,
                   (SELECT array_agg(a.attname ORDER BY k.ord) FROM unnest(con.conkey) WITH ORDINALITY k(attnum,ord) JOIN pg_attribute a ON a.attrelid=con.conrelid AND a.attnum=k.attnum) AS cols,
                   fn.nspname AS ref_schema, fc.relname AS ref_table,
                   (SELECT array_agg(a.attname ORDER BY k.ord) FROM unnest(con.confkey) WITH ORDINALITY k(attnum,ord) JOIN pg_attribute a ON a.attrelid=con.confrelid AND a.attnum=k.attnum) AS ref_cols
            FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
              LEFT JOIN pg_class fc ON fc.oid=con.confrelid LEFT JOIN pg_namespace fn ON fn.oid=fc.relnamespace
            WHERE con.contype IN ('p','u','f') AND n.nspname NOT IN ('pg_catalog','information_schema') AND (%(schemas)s::text[] IS NULL OR n.nspname = ANY(%(schemas)s))"""
    Q_ENUMS = "SELECT enumtypid AS typoid, array_agg(enumlabel ORDER BY enumsortorder) AS labels FROM pg_enum GROUP BY 1"
    Q_STATS = """SELECT schemaname AS schema, tablename AS tbl, attname AS name, n_distinct, most_common_vals::text::text[] AS vals FROM pg_stats
             WHERE schemaname NOT IN ('pg_catalog','information_schema') AND n_distinct > 0 AND n_distinct <= 25 AND most_common_vals IS NOT NULL"""

    def introspect(self, schemas=None):
        import psycopg
        sch = schemas or self.params.get('schemas') or None
        try:
            with self._connect(15) as c:
                tables = c.execute(self.Q_TABLES, {'schemas': sch}).fetchall()
                cols = c.execute(self.Q_COLUMNS, {'schemas': sch}).fetchall()
                cons = c.execute(self.Q_CONS, {'schemas': sch}).fetchall()
                enums = {r['typoid']: r['labels'] for r in c.execute(self.Q_ENUMS).fetchall()}
                try:
                    stats = {(r['schema'], r['tbl'], r['name']): r for r in c.execute(self.Q_STATS).fetchall()}
                except psycopg.Error:
                    stats = {}
        except psycopg.Error as e:
            raise DriverError(_one_line(e)) from e
        by = _new_tables([(t['schema'], t['name'], {'r': 'table', 'p': 'table', 'v': 'view', 'm': 'view'}[t['kind']], t['est_rows'], t['comment']) for t in tables])
        for col in cols:
            t = by.get((col['schema'], col['tbl']))
            if t is None:
                continue
            d = {'name': col['name'], 'type': col['type'], 'nullable': col['nullable'], 'comment': col['comment']}
            if col['typtype'] == 'e':
                d['values'] = enums.get(col['typoid'])
            else:
                st = stats.get((col['schema'], col['tbl'], col['name']))
                if st and st['vals']:
                    d['values'] = st['vals']
            t['columns'].append(d)
        for k in cons:
            _add_constraint(by, k['schema'], k['tbl'], {'p': 'pk', 'u': 'unique', 'f': 'fk'}[k['ctype']], k['cols'], k['ref_schema'], k['ref_table'], k['ref_cols'])
        return _catalog(by)


class BuiltinDriver(PostgresDriver):
    """The generated demo database: one login role per entitlement combination, with column grants (a second line behind enforce.py)."""

    def __init__(self):
        super().__init__({'params': {}, 'options': {}, 'secret': None})
        self._pools = {}

    def pool(self, ent=None):
        from . import access
        from .synth.load import role_name, role_url
        from psycopg.rows import dict_row
        role = role_name(access.normalize(ent) if ent is not None else access.DEFAULT)
        with self._lock:
            if role not in self._pools:
                self._pools[role] = ConnectionPool(role_url(role), min_size=0, max_size=4, kwargs={'row_factory': dict_row}, check=ConnectionPool.check_connection,
                                                   max_lifetime=900, open=True, timeout=10)
            return self._pools[role]

    def close(self):
        with self._lock:
            for p in self._pools.values():
                try:
                    p.close(timeout=2)
                except Exception:
                    pass
            self._pools.clear()


# =============================================================== shared catalog helpers
def _new_tables(items):
    return {(s, n): {'schema': s, 'name': n, 'kind': kind, 'rows': r if r is not None and r >= 0 else None, 'comment': c or None, 'columns': [], 'pk': [], 'uniques': [], 'fks': []}
            for s, n, kind, r, c in items}


def _add_constraint(by, schema, table, kind, cols, ref_schema=None, ref_table=None, ref_cols=None):
    t = by.get((schema, table))
    if t is None or not cols:
        return
    if kind == 'pk':
        t['pk'] = list(cols)
    elif kind == 'unique':
        t['uniques'].append(list(cols))
    else:
        t['fks'].append({'cols': list(cols), 'ref_schema': ref_schema or schema, 'ref_table': ref_table, 'ref_cols': list(ref_cols or [])})


def _catalog(by):
    return {'tables': list(by.values()), 'schemas': sorted({t['schema'] for t in by.values()}), 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}


# =============================================================== SQLite
def _safe_path(p):
    path = os.path.realpath(str(p or ''))
    if not path.startswith(FILES_DIR + os.sep):
        raise ValueError(f'the file must be inside {FILES_DIR} (the data folder mounted into the app)')
    if path.startswith(os.path.realpath(os.path.join(config.DATA_DIR, 'library')) + os.sep):
        raise ValueError('Library files cannot be used as a database')           # an admin must not read collection content through a connector
    if not os.path.isfile(path):
        raise ValueError('no such file')
    return path


class FileDriver(Driver):
    file_based = True
    fields = ('path', 'schemas')

    @classmethod
    def clean_params(cls, p):
        return {'path': _safe_path(p.get('path')), 'schemas': [s.strip() for s in (p.get('schemas') or []) if str(s).strip()]}

    def password(self):
        return None


class SqliteDriver(FileDriver):
    kind, label, dialect = 'sqlite', 'SQLite (file)', 'sqlite'
    hash_tpl = 'ss_mask(__c__)'

    def _conn(self):
        path = _safe_path(self.params['path'])
        c = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA query_only = ON')
        salt = mask_salt()
        c.create_function('ss_mask', 1, lambda v: None if v is None else 'h_' + hashlib.sha256((str(v) + salt).encode()).hexdigest()[:12], deterministic=True)
        deadline = time.monotonic() + self.options['statement_timeout_ms'] / 1000
        c.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        return c

    def to_sql(self, sql):
        return sql.replace('%s', '?')

    def run(self, sql, params=None, max_rows=None, ent=None):
        c = self._conn()
        try:
            cur = c.execute(self.to_sql(sql), list(params or []))
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(max_rows) if max_rows else cur.fetchall()
            return cols, [{k: _plain(r[k]) for k in cols} for r in rows]
        except sqlite3.Error as ex:
            raise DriverError(_one_line(ex)) from ex
        finally:
            c.close()

    def random_fn(self):
        return 'random()'

    def test(self):
        t0 = time.perf_counter()
        path = _safe_path(self.params['path'])
        c = self._conn()
        try:
            v = c.execute('select sqlite_version()').fetchone()[0]
            n = c.execute("select count(*) from sqlite_master where type in ('table','view') and name not like 'sqlite_%'").fetchone()[0]
        except sqlite3.Error as ex:
            raise DriverError(_one_line(ex)) from ex
        finally:
            c.close()
        warn = ['the file is writable by the app: the connection itself is read-only, but keep the file on a read-only mount or use a copy'] if os.access(path, os.W_OK) else []
        return {'version': f'SQLite {v}', 'user': '(file)', 'tables_visible': n, 'warnings': warn, 'ms': round((time.perf_counter() - t0) * 1000)}

    def introspect(self, schemas=None):
        c = self._conn()
        try:
            tabs = c.execute("select name, type from sqlite_master where type in ('table','view') and name not like 'sqlite_%' order by name").fetchall()
            by = _new_tables([('main', t['name'], 'view' if t['type'] == 'view' else 'table', None, None) for t in tabs])
            for t in tabs:
                n = t['name']
                q = '"' + n.replace('"', '""') + '"'
                pk = []
                for col in c.execute(f'PRAGMA table_info({q})').fetchall():
                    by[('main', n)]['columns'].append({'name': col['name'], 'type': col['type'] or 'text', 'nullable': not col['notnull'] and not col['pk'], 'comment': None})
                    if col['pk']:
                        pk.append((col['pk'], col['name']))
                _add_constraint(by, 'main', n, 'pk', [x[1] for x in sorted(pk)])
                fks = {}
                for fk in c.execute(f'PRAGMA foreign_key_list({q})').fetchall():
                    fks.setdefault(fk['id'], {'table': fk['table'], 'from': [], 'to': []})
                    fks[fk['id']]['from'].append(fk['from'])
                    fks[fk['id']]['to'].append(fk['to'])
                for fk in fks.values():
                    ref_pk = [r['name'] for r in sorted((x for x in c.execute(f'PRAGMA table_info("{fk["table"]}")').fetchall() if x['pk']), key=lambda x: x['pk'])]
                    _add_constraint(by, 'main', n, 'fk', fk['from'], 'main', fk['table'], [x for x in fk['to'] if x] or ref_pk)
                for ix in c.execute(f'PRAGMA index_list({q})').fetchall():
                    if ix['unique'] and ix['origin'] == 'u':
                        _add_constraint(by, 'main', n, 'unique', [r['name'] for r in c.execute(f'PRAGMA index_info("{ix["name"]}")').fetchall()])
            return _catalog(by)
        except sqlite3.Error as ex:
            raise DriverError(_one_line(ex)) from ex
        finally:
            c.close()


# =============================================================== DuckDB
class DuckDBDriver(FileDriver):
    kind, label, dialect = 'duckdb', 'DuckDB (file)', 'duckdb'
    hash_tpl = "'h_' || substr(sha256(CAST(__c__ AS VARCHAR) || '__salt__'), 1, 12)"

    def _conn(self):
        import duckdb
        path = _safe_path(self.params['path'])
        try:
            return duckdb.connect(path, read_only=True, config={'enable_external_access': False, 'autoinstall_known_extensions': False, 'autoload_known_extensions': False})
        except duckdb.Error as ex:
            raise DriverError(_one_line(ex)) from ex

    def to_sql(self, sql):
        return sql.replace('%s', '?')

    def run(self, sql, params=None, max_rows=None, ent=None):
        import duckdb
        c = self._conn()
        timer = threading.Timer(self.options['statement_timeout_ms'] / 1000, c.interrupt)
        timer.start()
        try:
            cur = c.execute(self.to_sql(sql), list(params or []))
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(max_rows) if max_rows else cur.fetchall()
            return cols, [dict(zip(cols, (_plain(v) for v in r))) for r in rows]
        except duckdb.Error as ex:
            raise DriverError(_one_line(ex)) from ex
        finally:
            timer.cancel()
            c.close()

    def test(self):
        import duckdb
        t0 = time.perf_counter()
        path = _safe_path(self.params['path'])
        c = self._conn()
        try:
            v = c.execute('select version()').fetchone()[0]
            n = c.execute("select count(*) from information_schema.tables where table_schema not in ('information_schema','pg_catalog')").fetchone()[0]
        except duckdb.Error as ex:
            raise DriverError(_one_line(ex)) from ex
        finally:
            c.close()
        warn = ['the file is writable by the app: the connection itself is read-only, but keep the file on a read-only mount or use a copy'] if os.access(path, os.W_OK) else []
        return {'version': f'DuckDB {v}', 'user': '(file)', 'tables_visible': n, 'warnings': warn, 'ms': round((time.perf_counter() - t0) * 1000)}

    def introspect(self, schemas=None):
        import duckdb
        sch = schemas or self.params.get('schemas') or None
        c = self._conn()
        try:
            def rows(sql, *a):
                cur = c.execute(sql, list(a))
                names = [d[0] for d in cur.description]
                return [dict(zip(names, r)) for r in cur.fetchall()]
            skip = ('information_schema', 'pg_catalog')
            tabs = rows("select t.table_schema s, t.table_name n, t.table_type ty, d.estimated_size est, d.comment cm from information_schema.tables t "
                        "left join duckdb_tables() d on d.schema_name=t.table_schema and d.table_name=t.table_name where t.table_schema not in ('information_schema','pg_catalog') order by 1,2")
            tabs = [t for t in tabs if not sch or t['s'] in sch]
            by = _new_tables([(t['s'], t['n'], 'view' if 'VIEW' in (t['ty'] or '') else 'table', t['est'], t['cm']) for t in tabs])
            for col in rows("select table_schema s, table_name t, column_name n, data_type ty, is_nullable nl, column_default d from information_schema.columns "
                            "where table_schema not in ('information_schema','pg_catalog') order by table_schema, table_name, ordinal_position"):
                t = by.get((col['s'], col['t']))
                if t is not None:
                    t['columns'].append({'name': col['n'], 'type': col['ty'], 'nullable': col['nl'] == 'YES', 'comment': None})
            for k in rows("select schema_name s, table_name t, constraint_type ty, constraint_column_names cols, referenced_table rt, referenced_column_names rcols from duckdb_constraints()"):
                kind = {'PRIMARY KEY': 'pk', 'UNIQUE': 'unique', 'FOREIGN KEY': 'fk'}.get(k['ty'])
                if kind:
                    _add_constraint(by, k['s'], k['t'], kind, k['cols'], k['s'], k['rt'], k['rcols'])
            return _catalog(by)
        except duckdb.Error as ex:
            raise DriverError(_one_line(ex)) from ex
        finally:
            c.close()


# =============================================================== MySQL / MariaDB
class MySQLDriver(Driver):
    kind, label, dialect, default_port = 'mysql', 'MySQL', 'mysql', 3306
    qchar = '`'
    hash_tpl = "CONCAT('h_', SUBSTRING(SHA2(CONCAT(CAST(__c__ AS CHAR), '__salt__'), 256), 1, 12))"

    @classmethod
    def clean_params(cls, p):
        return super().clean_params(p)                                # the database is optional: schemas (= MySQL databases) say what to read

    def _connect(self, timeout=10):
        import pymysql
        from pymysql.cursors import DictCursor
        p = self.params
        ssl = {}
        kw = {}
        if p['sslmode'] == 'disable':
            kw['ssl_disabled'] = True
        elif p['sslmode'] in ('require', 'verify-ca', 'verify-full'):
            ssl = {'check_hostname': p['sslmode'] == 'verify-full'}
            kw.update(ssl=ssl, ssl_verify_cert=p['sslmode'] != 'require', ssl_verify_identity=p['sslmode'] == 'verify-full')
        try:
            c = pymysql.connect(host=p['host'], port=p['port'], user=p['user'], password=self.password() or '', database=p.get('database') or None, connect_timeout=timeout,
                                read_timeout=max(5, self.options['statement_timeout_ms'] // 1000 + 5), cursorclass=DictCursor, autocommit=True, charset='utf8mb4', **kw)
        except pymysql.Error as ex:
            raise DriverError(_one_line(ex)) from ex
        with c.cursor() as cur:
            cur.execute('SET SESSION TRANSACTION READ ONLY')
            for stmt in (f"SET SESSION MAX_EXECUTION_TIME={int(self.options['statement_timeout_ms'])}", f"SET SESSION max_statement_time={self.options['statement_timeout_ms'] / 1000}"):
                try:
                    cur.execute(stmt)                              # MySQL has the first, MariaDB the second
                except pymysql.Error:
                    pass
        return c

    def run(self, sql, params=None, max_rows=None, ent=None):
        import pymysql
        c = self._connect()
        try:
            with c.cursor() as cur:
                cur.execute(sql, list(params) if params else None)
                if not cur.description:
                    return [], []
                cols = [d[0] for d in cur.description]
                rows = cur.fetchmany(max_rows) if max_rows else cur.fetchall()
                return cols, [{k: _plain(v) for k, v in r.items()} for r in rows]
        except pymysql.err.OperationalError as ex:
            raise DriverError(_one_line(ex), privilege=ex.args and ex.args[0] in (1044, 1045, 1142, 1143)) from ex
        except pymysql.Error as ex:
            raise DriverError(_one_line(ex)) from ex
        finally:
            c.close()

    def cast_text(self, expr):
        return f'CAST({expr} AS CHAR)'

    def random_fn(self):
        return 'RAND()'

    SYSTEM_DBS = ('mysql', 'sys', 'information_schema', 'performance_schema')

    def _dbs(self, schemas):
        sch = schemas or self.params.get('schemas') or ([self.params['database']] if self.params.get('database') else None)
        return sch

    def test(self):
        import pymysql
        t0 = time.perf_counter()
        c = self._connect()
        try:
            with c.cursor() as cur:
                cur.execute('SELECT VERSION() AS v, CURRENT_USER() AS u')
                me = cur.fetchone()
                sch = self._dbs(None)
                if sch:
                    cur.execute('SELECT count(*) AS n FROM information_schema.tables WHERE table_schema IN %s', (tuple(sch),))
                else:
                    cur.execute("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema NOT IN ('mysql','sys','information_schema','performance_schema')")
                n = cur.fetchone()['n']
                warn = []
                try:
                    cur.execute('SHOW GRANTS')
                    grants = ' '.join(str(list(r.values())[0]) for r in cur.fetchall()).upper()
                    if re.search(r'ALL PRIVILEGES|\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|FILE|SUPER)\b', grants):
                        warn.append('the account has write or administrative privileges: use a read-only account (queries are forced read-only, but a read-only account is the real protection)')
                except pymysql.Error:
                    pass
        except pymysql.Error as ex:
            raise DriverError(_one_line(ex)) from ex
        finally:
            c.close()
        return {'version': f"{'MariaDB' if 'mariadb' in me['v'].lower() else 'MySQL'} {me['v']}", 'user': me['u'], 'tables_visible': n, 'warnings': warn, 'ms': round((time.perf_counter() - t0) * 1000)}

    def introspect(self, schemas=None):
        import pymysql
        sch = self._dbs(schemas)
        flt = ('IN %s', (tuple(sch),)) if sch else ("NOT IN ('mysql','sys','information_schema','performance_schema')", ())
        c = self._connect(15)
        try:
            with c.cursor() as cur:
                def rows(sql, args=()):
                    cur.execute(sql, args)
                    return cur.fetchall()
                tabs = rows(f"SELECT TABLE_SCHEMA s, TABLE_NAME n, TABLE_TYPE ty, TABLE_ROWS est, TABLE_COMMENT cm FROM information_schema.TABLES WHERE TABLE_SCHEMA {flt[0]} ORDER BY 1,2", flt[1])
                by = _new_tables([(t['s'], t['n'], 'view' if 'VIEW' in t['ty'] else 'table', t['est'], t['cm']) for t in tabs])
                for col in rows(f"SELECT TABLE_SCHEMA s, TABLE_NAME t, COLUMN_NAME n, COLUMN_TYPE ty, IS_NULLABLE nl, COLUMN_COMMENT cm FROM information_schema.COLUMNS "
                                f"WHERE TABLE_SCHEMA {flt[0]} ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION", flt[1]):
                    t = by.get((col['s'], col['t']))
                    if t is not None:
                        d = {'name': col['n'], 'type': col['ty'], 'nullable': col['nl'] == 'YES', 'comment': col['cm'] or None}
                        m = re.match(r"(?i)enum\((.*)\)$", col['ty'])
                        if m:
                            d['values'] = re.findall(r"'((?:[^']|'')*)'", m.group(1))
                        t['columns'].append(d)
                # KEY_COLUMN_USAGE alone: TABLE_CONSTRAINTS is hidden from accounts that only have SELECT. PRIMARY = primary key, a referenced table = foreign key, the rest unique.
                cons = rows(f"SELECT TABLE_SCHEMA s, TABLE_NAME t, CONSTRAINT_NAME cn, COLUMN_NAME col, REFERENCED_TABLE_SCHEMA rs, REFERENCED_TABLE_NAME rt, REFERENCED_COLUMN_NAME rc "
                            f"FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA {flt[0]} ORDER BY TABLE_SCHEMA, TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION", flt[1])
                groups = {}
                for r in cons:
                    g = groups.setdefault((r['s'], r['t'], r['cn']), {'cols': [], 'rs': r['rs'], 'rt': r['rt'], 'rc': []})
                    g['cols'].append(r['col'])
                    if r['rc']:
                        g['rc'].append(r['rc'])
                for (s_, t, cn), g in groups.items():
                    kind = 'pk' if cn == 'PRIMARY' else 'fk' if g['rt'] else 'unique'
                    _add_constraint(by, s_, t, kind, g['cols'], g['rs'], g['rt'], g['rc'])
            return _catalog(by)
        except pymysql.Error as ex:
            raise DriverError(_one_line(ex)) from ex
        finally:
            c.close()


class MariaDBDriver(MySQLDriver):
    kind, label = 'mariadb', 'MariaDB'


# =============================================================== ClickHouse
class ClickHouseDriver(Driver):
    kind, label, dialect, default_port = 'clickhouse', 'ClickHouse', 'clickhouse', 8123
    hash_tpl = "concat('h_', substring(lower(hex(SHA256(concat(toString(__c__), '__salt__')))), 1, 12))"

    @classmethod
    def clean_params(cls, p):
        return super().clean_params(p)

    def _client(self):
        import clickhouse_connect
        p = self.params
        secs = max(1, self.options['statement_timeout_ms'] // 1000)
        try:
            return clickhouse_connect.get_client(host=p['host'], port=p['port'], username=p['user'], password=self.password() or '', database=p.get('database') or 'default',
                                                 secure=p['sslmode'] in ('require', 'verify-ca', 'verify-full'), verify=p['sslmode'] in ('verify-ca', 'verify-full'),
                                                 connect_timeout=10, send_receive_timeout=secs + 10, autogenerate_session_id=False,
                                                 settings={'readonly': 2, 'max_execution_time': secs, 'max_result_rows': 100000, 'result_overflow_mode': 'break', 'max_memory_usage': 2_000_000_000})
        except Exception as ex:
            raise DriverError(_one_line(ex)) from ex

    def run(self, sql, params=None, max_rows=None, ent=None):
        c = self._client()
        try:
            res = c.query(sql, parameters=list(params) if params else None)
            cols = list(res.column_names)
            rows = res.result_rows[:max_rows] if max_rows else res.result_rows
            return cols, [dict(zip(cols, (_plain(v) for v in r))) for r in rows]
        except Exception as ex:
            msg = _one_line(ex)
            raise DriverError(msg, privilege='ACCESS_DENIED' in str(ex) or 'Not enough privileges' in str(ex)) from ex
        finally:
            c.close()

    def cast_text(self, expr):
        return f'toString({expr})'

    def random_fn(self):
        return 'rand()'

    def count_capped(self, qualified, cap):
        return f'SELECT count() AS n FROM (SELECT 1 FROM {qualified} LIMIT {cap})'

    def test(self):
        t0 = time.perf_counter()
        c = self._client()
        try:
            v = c.query('SELECT version(), currentUser()').result_rows[0]
            sch = self.params.get('schemas') or ([self.params['database']] if self.params.get('database') else None)
            if sch:
                n = c.query('SELECT count() FROM system.tables WHERE database IN %s', parameters=[tuple(sch)]).result_rows[0][0]
            else:
                n = c.query("SELECT count() FROM system.tables WHERE database NOT IN ('system','INFORMATION_SCHEMA','information_schema')").result_rows[0][0]
            warn = []
            try:
                g = c.query("SELECT groupUniqArray(access_type) FROM system.grants WHERE user_name = currentUser() AND access_type IN ('INSERT','ALTER','CREATE','DROP','TRUNCATE','ALL')").result_rows[0][0]
                if g:
                    warn.append(f'the account has write privileges ({", ".join(sorted(g))}): use a read-only account (queries run with readonly=2, but a read-only account is the real protection)')
            except Exception:
                pass
        except DriverError:
            raise
        except Exception as ex:
            raise DriverError(_one_line(ex)) from ex
        finally:
            c.close()
        return {'version': f'ClickHouse {v[0]}', 'user': v[1], 'tables_visible': n, 'warnings': warn, 'ms': round((time.perf_counter() - t0) * 1000)}

    def introspect(self, schemas=None):
        sch = schemas or self.params.get('schemas') or ([self.params['database']] if self.params.get('database') else None)
        c = self._client()
        try:
            where = 'database IN %(s)s' if sch else "database NOT IN ('system','INFORMATION_SCHEMA','information_schema')"
            pr = {'s': tuple(sch)} if sch else {}
            tabs = c.query(f'SELECT database, name, engine, total_rows, comment FROM system.tables WHERE {where} AND NOT is_temporary ORDER BY 1,2', parameters=pr).result_rows
            by = _new_tables([(d, n, 'view' if 'View' in e else 'table', r, cm) for d, n, e, r, cm in tabs])
            pks = {}
            for d, t, n, ty, cm, pk in c.query(f'SELECT database, table, name, type, comment, is_in_primary_key FROM system.columns WHERE {where} ORDER BY database, table, position', parameters=pr).result_rows:
                tb = by.get((d, t))
                if tb is None:
                    continue
                nullable = ty.startswith('Nullable(') or 'Nullable(' in ty
                x = {'name': n, 'type': ty, 'nullable': nullable, 'comment': cm or None}
                m = re.match(r"(?:LowCardinality\()?Enum\d*\((.*)\)\)?$", ty)
                if m:
                    x['values'] = re.findall(r"'((?:[^'\\]|\\.)*)'\s*=", m.group(1))
                tb['columns'].append(x)
                if pk:
                    pks.setdefault((d, t), []).append(n)
            for (d, t), cols in pks.items():
                _add_constraint(by, d, t, 'pk', cols)                 # a ClickHouse primary key is a sort key, not a uniqueness guarantee: reverse.py notes it
            return _catalog(by)
        except Exception as ex:
            raise DriverError(_one_line(ex)) from ex
        finally:
            c.close()


KINDS = {c.kind: c for c in (PostgresDriver, MySQLDriver, MariaDBDriver, SqliteDriver, DuckDBDriver, ClickHouseDriver)}
_cache = {}
_lock = threading.Lock()


def driver_for(row):
    """The (cached) driver of a connector row; a changed connector gets a fresh one and the old one is closed."""
    key = row['id']
    sig = hash((row['kind'], str(row['params']), row.get('secret'), str(row.get('options'))))
    with _lock:
        old = _cache.get(key)
        if old and old[0] == sig:
            return old[1]
        if old:
            old[1].close()
        if row['kind'] not in KINDS:
            raise DriverError(f"unknown connector kind {row['kind']}")
        d = KINDS[row['kind']](row)
        _cache[key] = (sig, d)
        return d


def close_all():
    with _lock:
        for _, d in _cache.values():
            d.close()
        _cache.clear()
