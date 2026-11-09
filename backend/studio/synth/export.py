"""Copy the built-in demo database into other database kinds, so each connector can be tried on the same data.

    python -m studio.synth.export sqlite  /app/data/samples/demo.sqlite
    python -m studio.synth.export duckdb  /app/data/samples/demo.duckdb
    python -m studio.synth.export mariadb host=sampledb-maria port=3306 user=root password=... database_prefix=
    python -m studio.synth.export clickhouse host=sampledb-ch port=8123 user=default password=...

Reads the built-in database (as the full role), writes with the target's own native library and an account that may write. SQLite has no schemas: its
tables are flat (it is the demo for "generate a model from this database"); the others keep the schemas of the model.
"""
import json
import os
import sys
import time

from .. import access, databuild, drivers
from . import ddl

BATCH = 1000


def _model():
    cat, _b = databuild.catalog()
    return cat.onto


def _rows(onto):
    """{entity key: (columns, rows as lists)} read from the built-in database; JSON-able arrays stay Python lists."""
    out = {}
    drv = drivers.BuiltinDriver()                                 # always the built-in database, whatever connector is active
    for k, e in onto['entities'].items():
        cols = list(e['attributes'])
        _c, rows = drv.run(f"SELECT {', '.join(ddl.q(c) for c in cols)} FROM {ddl.qualified(e)}", None, None, access.FULL)
        out[k] = (cols, [[r[c] for c in cols] for r in rows])
    drv.close()
    return out


def _keys(e):
    return set(ddl.pk_cols(e))


TYPES = {
    'mysql': {'text': 'TEXT', 'integer': 'BIGINT', 'decimal': 'DECIMAL(18,2)', 'decimal (ratio)': 'DECIMAL(9,4)', 'boolean': 'BOOLEAN', 'date': 'DATE', 'timestamp': 'DATETIME(3)', 'text[]': 'JSON'},
    'sqlite': {'text': 'TEXT', 'integer': 'INTEGER', 'decimal': 'NUMERIC', 'decimal (ratio)': 'NUMERIC', 'boolean': 'INTEGER', 'date': 'TEXT', 'timestamp': 'TEXT', 'text[]': 'TEXT'},
    'duckdb': {'text': 'VARCHAR', 'integer': 'BIGINT', 'decimal': 'DECIMAL(18,2)', 'decimal (ratio)': 'DECIMAL(9,4)', 'boolean': 'BOOLEAN', 'date': 'DATE', 'timestamp': 'TIMESTAMP', 'text[]': 'VARCHAR[]'},
    'clickhouse': {'text': 'String', 'integer': 'Int64', 'decimal': 'Decimal(18,2)', 'decimal (ratio)': 'Decimal(9,4)', 'boolean': 'Bool', 'date': 'Date', 'timestamp': 'DateTime64(3)', 'text[]': 'Array(String)'},
}


def _fk_cols(onto):
    fks = ddl.foreign_keys(onto)
    keyish = {(k, c) for k, c, _t in fks} | {(t, ddl.pk_col(onto['entities'][t])) for _k, _c, t in fks}
    return fks, keyish


def _table_ddl(kind, onto, e, k, keyish, fks, qname, with_fks):
    t = TYPES[kind]
    cols, pk = [], _keys(e)
    for c, a in e['attributes'].items():
        ty = t.get(a['type'], t['text'])
        if kind == 'mysql' and ty == 'TEXT' and (c in pk or (k, c) in keyish):
            ty = 'VARCHAR(100)'
        if kind == 'clickhouse' and c not in pk and a['type'] != 'text[]':
            ty = f'Nullable({ty})'
        cols.append(f"  {qname(c)} {ty}" + (' NOT NULL' if c in pk and kind != 'clickhouse' else ''))
    if kind == 'clickhouse':
        return f"CREATE TABLE {{db}}.{qname(ddl.table_of(e))} (\n" + ',\n'.join(cols) + f"\n) ENGINE = MergeTree ORDER BY ({', '.join(qname(c) for c in ddl.pk_cols(e))})"
    cols.append(f"  PRIMARY KEY ({', '.join(qname(c) for c in ddl.pk_cols(e))})")
    if with_fks:
        for fk_k, fk_c, t_k in fks:
            if fk_k == k:
                te = onto['entities'][t_k]
                cols.append(f"  FOREIGN KEY ({qname(fk_c)}) REFERENCES {{ref_{t_k}}} ({qname(ddl.pk_col(te))})")
    return f"CREATE TABLE {{tbl_{k}}} (\n" + ',\n'.join(cols) + '\n)'


def _cell(kind, v):
    if isinstance(v, list):
        return v if kind in ('duckdb', 'clickhouse') else json.dumps(v)
    if kind == 'sqlite':
        if isinstance(v, bool):
            return int(v)
        if hasattr(v, 'hour'):
            return v.isoformat(sep=' ')
        if hasattr(v, 'isoformat'):
            return v.isoformat()
        if hasattr(v, 'as_tuple'):
            return float(v)
    return v


def to_sqlite(path):
    import sqlite3
    onto = _model()
    data = _rows(onto)
    fks, keyish = _fk_cols(onto)
    if os.path.exists(path):
        os.remove(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path)
    names = {k: f'"{ddl.table_of(e)}"' for k, e in onto['entities'].items()}
    for k, e in onto['entities'].items():
        sql = _table_ddl('sqlite', onto, e, k, keyish, fks, lambda n: f'"{n}"', True)
        sql = sql.replace(f'{{tbl_{k}}}', names[k])
        for t_k in onto['entities']:
            sql = sql.replace(f'{{ref_{t_k}}}', names[t_k])
        c.execute(sql)
    for k, (cols, rows) in data.items():
        c.executemany(f"INSERT INTO {names[k]} VALUES ({', '.join('?' * len(cols))})", [[_cell('sqlite', v) for v in r] for r in rows])
    c.commit()
    c.close()
    return {k: len(v[1]) for k, v in data.items()}


def to_duckdb(path):
    import duckdb
    onto = _model()
    data = _rows(onto)
    fks, keyish = _fk_cols(onto)
    if os.path.exists(path):
        os.remove(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = duckdb.connect(path)
    for s in sorted({ddl.schema_of(e) for e in onto['entities'].values()}):
        c.execute(f'CREATE SCHEMA IF NOT EXISTS "{s}"')
    q = lambda n: f'"{n}"'
    for k, e in onto['entities'].items():
        sql = _table_ddl('duckdb', onto, e, k, keyish, fks, q, False).replace(f'{{tbl_{k}}}', ddl.qualified(e))      # foreign keys are not declared: DuckDB cannot load self-referencing rows in one pass
        c.execute(sql)
    for k, (cols, rows) in data.items():
        e = onto['entities'][k]
        for i in range(0, len(rows), BATCH):
            c.executemany(f"INSERT INTO {ddl.qualified(e)} VALUES ({', '.join('?' * len(cols))})", rows[i:i + BATCH])
    for k, e in onto['entities'].items():
        for col, a in e['attributes'].items():
            if a.get('description'):
                c.execute(f"COMMENT ON COLUMN {ddl.qualified(e)}.\"{col}\" IS '{a['description'][:300].replace(chr(39), chr(39) * 2)}'")
    c.close()
    return {k: len(v[1]) for k, v in data.items()}


def to_mysql(kind, host, port, user, password):
    import pymysql
    onto = _model()
    data = _rows(onto)
    fks, keyish = _fk_cols(onto)
    c = pymysql.connect(host=host, port=int(port), user=user, password=password, charset='utf8mb4', autocommit=False)
    cur = c.cursor()
    cur.execute('SET FOREIGN_KEY_CHECKS=0')
    for s in sorted({ddl.schema_of(e) for e in onto['entities'].values()}):
        cur.execute(f'DROP DATABASE IF EXISTS `{s}`')
        cur.execute(f'CREATE DATABASE `{s}` CHARACTER SET utf8mb4')
    q = lambda n: f'`{n}`'
    for k, e in onto['entities'].items():
        sql = _table_ddl('mysql', onto, e, k, keyish, fks, q, True).replace(f'{{tbl_{k}}}', f'`{ddl.schema_of(e)}`.`{ddl.table_of(e)}`')
        for t_k, te in onto['entities'].items():
            sql = sql.replace(f'{{ref_{t_k}}}', f'`{ddl.schema_of(te)}`.`{ddl.table_of(te)}`')
        cur.execute(sql)
    for k, (cols, rows) in data.items():
        e = onto['entities'][k]
        ins = f"INSERT INTO `{ddl.schema_of(e)}`.`{ddl.table_of(e)}` VALUES ({', '.join(['%s'] * len(cols))})"
        for i in range(0, len(rows), BATCH):
            cur.executemany(ins, [[_cell('mysql', v) for v in r] for r in rows[i:i + BATCH]])
    cur.execute('SET FOREIGN_KEY_CHECKS=1')
    c.commit()
    c.close()
    return {k: len(v[1]) for k, v in data.items()}


def to_clickhouse(host, port, user, password):
    import clickhouse_connect
    onto = _model()
    data = _rows(onto)
    fks, keyish = _fk_cols(onto)
    cl = clickhouse_connect.get_client(host=host, port=int(port), username=user, password=password)
    for s in sorted({ddl.schema_of(e) for e in onto['entities'].values()}):
        cl.command(f'DROP DATABASE IF EXISTS `{s}`')
        cl.command(f'CREATE DATABASE `{s}`')
    for k, e in onto['entities'].items():
        cl.command(_table_ddl('clickhouse', onto, e, k, keyish, fks, lambda n: f'`{n}`', False).replace('{db}', f'`{ddl.schema_of(e)}`'))
    for k, (cols, rows) in data.items():
        e = onto['entities'][k]
        for i in range(0, len(rows), 5000):
            cl.insert(f'{ddl.schema_of(e)}.{ddl.table_of(e)}', rows[i:i + 5000], column_names=cols)
    cl.close()
    return {k: len(v[1]) for k, v in data.items()}


if __name__ == '__main__':
    kind, args = sys.argv[1], sys.argv[2:]
    kw = dict(a.split('=', 1) for a in args if '=' in a)
    t0 = time.time()
    if kind == 'sqlite':
        n = to_sqlite(args[0])
    elif kind == 'duckdb':
        n = to_duckdb(args[0])
    elif kind in ('mysql', 'mariadb'):
        n = to_mysql(kind, kw['host'], kw.get('port', 3306), kw['user'], kw['password'])
    elif kind == 'clickhouse':
        n = to_clickhouse(kw['host'], kw.get('port', 8123), kw['user'], kw['password'])
    else:
        raise SystemExit('kind: sqlite | duckdb | mariadb | mysql | clickhouse')
    print(f'{kind}: {sum(n.values())} rows in {len(n)} tables, {time.time() - t0:.1f}s')
