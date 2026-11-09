import json
from pathlib import Path
from psycopg.rows import dict_row
from psycopg.types.json import Json, Jsonb
from psycopg_pool import ConnectionPool
from . import config

_pool = None


def pool():
    global _pool
    if _pool is None:
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=8, kwargs={'row_factory': dict_row}, check=ConnectionPool.check_connection, max_lifetime=1800, open=True)
    return _pool


def conn():
    """Context manager yielding a connection (commits on success)."""
    return pool().connection()


def init_schema():
    sql = (Path(__file__).parent / 'schema.sql').read_text().replace('__DIM__', str(int(config.EMBEDDING_DIM)))      # a new database gets the configured size
    lib = (Path(__file__).parent / 'library' / 'schema.sql').read_text()
    with conn() as c:
        c.execute(sql)
        c.execute(lib)
        from . import vectors
        vectors.ensure_embedding_column(c, 'library.topic', 'topic_embedding')
        vectors.ensure_embedding_column(c, 'library.claim', 'claim_embedding')
        vectors.ensure_library_indexes(c)
        if vectors.ensure_library_chunks(c):
            print('library passages converted to the new embedding size: vectors will be recomputed', flush=True)
        if vectors.ensure_dimension(c):                                  # an existing database made for another size: empty the vectors, the backfill refills them
            print(f'embedding dimension changed to {config.EMBEDDING_DIM}: vectors will be recomputed in the background', flush=True)


def J(v):
    return Json(v)
