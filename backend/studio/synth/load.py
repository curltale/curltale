"""Create the synthetic database from the published model, load the generated world, validate it."""
import hashlib
import re
import time
import psycopg

from .. import access, config
from ..db import conn
from . import ddl
from .generic import Generator, ai_suggester

DB_NAME = config.DATA_DB
from itertools import product

COMBOS = [dict(zip(('confidential', 'personal', 'restricted'), bits)) for bits in product((True, False), repeat=3)]


def role_name(ent):
    return 'sd_' + access.role_code(ent)


def _url(db):
    return re.sub(r'/[^/?]+(\?|$)', f'/{db}\\1', config.DATABASE_URL, count=1)


def role_password(role):
    return hashlib.sha256(f'{config.SECRET_KEY}:{role}'.encode()).hexdigest()[:32]


def role_url(role):
    """Connection string of a read-only login role (sd_ro cannot see restricted columns at all)."""
    base = _url(DB_NAME)
    return re.sub(r'//[^@]+@', f'//{role}:{role_password(role)}@', base, count=1)


def _lit(s):
    return "'" + s.replace("'", "''") + "'"


def generator(onto, seed=7, scale='medium', ai=False):
    """The demo-data generator for this model; with ai=True (and a chat model configured) it asks for realistic values first."""
    suggest = None
    if ai:
        from .. import llm
        if llm.available():
            suggest = ai_suggester(llm.complete)
    return Generator(onto, seed=seed, scale=scale, suggest=suggest)


def build(onto, seed=7, scale='medium', progress=lambda m: None, ai=False, gen=None):
    t0 = time.time()
    gen = gen or generator(onto, seed, scale, ai)
    rows = gen.build(progress)
    ents = onto['entities']
    counts = {ddl.table_of(e): len(rows[k]) for k, e in ents.items()}

    progress('creating database and roles')
    with psycopg.connect(_url('postgres'), autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)')
        c.execute(f'CREATE DATABASE {DB_NAME}')
        roles = [role_name(e) for e in COMBOS]
        for role in roles:
            exists = c.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone()
            if exists:      # only a superuser may restate SUPERUSER/CREATEDB/CREATEROLE: an existing role keeps the ones it was created with (none)
                c.execute(f"ALTER ROLE {role} LOGIN PASSWORD {_lit(role_password(role))} CONNECTION LIMIT 8")
            else:
                c.execute(f"CREATE ROLE {role} LOGIN PASSWORD {_lit(role_password(role))} NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 8")
            c.execute(f"ALTER ROLE {role} SET default_transaction_read_only = on")
            c.execute(f"ALTER ROLE {role} SET statement_timeout = '8s'")
            c.execute(f"ALTER ROLE {role} SET idle_in_transaction_session_timeout = '15s'")
        c.execute(f'REVOKE ALL ON DATABASE {DB_NAME} FROM PUBLIC')
        c.execute(f'GRANT CONNECT ON DATABASE {DB_NAME} TO {", ".join(roles)}')

    with psycopg.connect(_url(DB_NAME)) as c:
        progress('creating tables')
        for s in ddl.build_tables_sql(onto):
            c.execute(s)
        progress('loading rows')
        for k, e in ents.items():
            cols = list(e['attributes'])
            with c.cursor() as cur, cur.copy(f"COPY {ddl.qualified(e)} ({', '.join(ddl.q(x) for x in cols)}) FROM STDIN") as cp:
                for row in rows[k]:
                    cp.write_row([row.get(x) for x in cols])
        progress('checking foreign keys')
        bad = []
        for label, sql in ddl.orphan_sql(onto):
            n = c.execute(sql).fetchone()[0]
            if n:
                bad.append(f'{label}: {n} orphan rows')
        if bad:
            raise RuntimeError('generated data violates foreign keys:\n' + '\n'.join(bad))
        progress('adding constraints, indexes, comments, grants')
        for s in ddl.fk_sql(onto) + ddl.index_sql(onto) + ddl.comments_sql(onto):
            c.execute(s)
        for combo in COMBOS:
            for s in ddl.grants_sql(onto, role_name(combo), combo):
                c.execute(s)
        c.commit()
        c.autocommit = True
        c.execute('ANALYZE')
    return {'seconds': round(time.time() - t0, 1), 'counts': counts, 'rows': sum(counts.values()), 'seed': seed, 'scale': scale, 'ai': bool(gen.suggest), 'findings': gen.findings}


def drop_builtin():
    """Remove the built-in demo database and its login roles (it can be rebuilt from the published model at any time)."""
    with psycopg.connect(_url('postgres'), autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)')
        for e in COMBOS:
            c.execute(f'DROP ROLE IF EXISTS {role_name(e)}')
