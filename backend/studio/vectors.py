"""Vector columns for any embedding dimension. pgvector can index `vector` up to 2000 dimensions and `halfvec` (half precision) up to 4000;
beyond that a search scans the rows (exact, slower on big tables). The Library builds its tables from `spec`, so the embedding model can be
chosen freely."""
from . import config

HNSW_VECTOR_MAX, HNSW_HALFVEC_MAX = 2000, 4000


def spec(dim):
    """How to store vectors of this size: {'type': 'vector(768)', 'ops': 'vector_cosine_ops' | None, 'cast': 'vector(768)'} (ops None: no index possible)."""
    if dim < 1 or dim > 16000:
        raise ValueError(f'embedding dimension {dim} is outside what pgvector stores (1 to 16000)')
    if dim <= HNSW_VECTOR_MAX:
        return {'type': f'vector({dim})', 'ops': 'vector_cosine_ops', 'cast': f'vector({dim})'}
    if dim <= HNSW_HALFVEC_MAX:
        return {'type': f'halfvec({dim})', 'ops': 'halfvec_cosine_ops', 'cast': f'halfvec({dim})'}
    return {'type': f'halfvec({dim})', 'ops': None, 'cast': f'halfvec({dim})'}


def column_dim(c, table, column='embedding', schema='public'):
    """The dimension the column really has now (None: no such column or no fixed size)."""
    r = c.execute("SELECT a.atttypmod AS m FROM pg_attribute a JOIN pg_class t ON t.oid=a.attrelid JOIN pg_namespace n ON n.oid=t.relnamespace "
                  "WHERE n.nspname=%s AND t.relname=%s AND a.attname=%s AND NOT a.attisdropped", (schema, table, column)).fetchone()
    return r['m'] if r and r['m'] > 0 else None


def ensure_dimension(c, dim=None):
    """Make the model's own vector columns match the configured dimension. The search index is rebuilt from the text (vectors are derived data):
    the chunk vectors are emptied, to be filled again by the background backfill, and the embedding cache is dropped. Returns True if anything changed."""
    dim = dim or config.EMBEDDING_DIM
    changed = False
    have = column_dim(c, 'chunk')
    if have is not None and have != dim:
        c.execute(f'ALTER TABLE chunk ALTER COLUMN embedding TYPE vector({dim}) USING NULL')
        changed = True
    have = column_dim(c, 'embedding_cache')
    if have is not None and have != dim:
        c.execute('TRUNCATE embedding_cache')
        c.execute(f'ALTER TABLE embedding_cache ALTER COLUMN embedding TYPE vector({dim}) USING NULL')
        changed = True
    return changed


def ensure_library_chunks(c, dim=None):
    """The Library's passages table, with a vector column of the configured size and an HNSW index where pgvector allows one.
    A table made for another size is converted: its vectors are emptied (the ingestion backfill recomputes them) and the index rebuilt."""
    dim = dim or config.EMBEDDING_DIM
    sp = spec(dim)
    c.execute(f"""CREATE TABLE IF NOT EXISTS library.source_chunk (
      id            bigserial PRIMARY KEY,
      collection_id uuid NOT NULL,
      source_id     uuid NOT NULL,
      idx           int NOT NULL,
      text          text NOT NULL,
      tokens        int NOT NULL,
      char_start    int,
      char_end      int,
      heading_path  text[] NOT NULL DEFAULT '{{}}',
      location      text,
      embedding     {sp['type']},
      tsv           tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
      UNIQUE (source_id, idx),
      FOREIGN KEY (source_id, collection_id) REFERENCES library.source (id, collection_id) ON DELETE CASCADE
    )""")
    c.execute('CREATE INDEX IF NOT EXISTS source_chunk_scope ON library.source_chunk (collection_id, source_id, idx)')
    c.execute('CREATE INDEX IF NOT EXISTS source_chunk_tsv ON library.source_chunk USING gin (tsv)')
    have = c.execute("SELECT format_type(a.atttypid, a.atttypmod) AS t FROM pg_attribute a WHERE a.attrelid='library.source_chunk'::regclass AND a.attname='embedding'").fetchone()['t']
    changed = have != sp['type']
    if changed:
        c.execute('DROP INDEX IF EXISTS library.source_chunk_embedding')
        c.execute(f"ALTER TABLE library.source_chunk ALTER COLUMN embedding TYPE {sp['type']} USING NULL")
    if sp['ops']:
        c.execute(f"CREATE INDEX IF NOT EXISTS source_chunk_embedding ON library.source_chunk USING hnsw (embedding {sp['ops']})")
    return changed


def _chunk_table(c, table, owner_cols, fk, sp):
    """A chunk table (text, place, vector, words) of the configured vector size; converted (vectors emptied) when the size changed. -> changed"""
    schema, name = table.split('.')
    c.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
      id            bigserial PRIMARY KEY,
      collection_id uuid NOT NULL,
      {owner_cols},
      idx           int NOT NULL,
      text          text NOT NULL,
      tokens        int NOT NULL,
      heading_path  text[] NOT NULL DEFAULT '{{}}',
      embedding     {sp['type']},
      tsv           tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
      {fk}
    )""")
    c.execute(f'CREATE INDEX IF NOT EXISTS {name}_tsv ON {table} USING gin (tsv)')
    have = c.execute("SELECT format_type(a.atttypid, a.atttypmod) AS t FROM pg_attribute a WHERE a.attrelid=%s::regclass AND a.attname='embedding'", (table,)).fetchone()['t']
    changed = have != sp['type']
    if changed:
        c.execute(f'DROP INDEX IF EXISTS {schema}.{name}_embedding')
        c.execute(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE {sp['type']} USING NULL")
    if sp['ops']:
        c.execute(f"CREATE INDEX IF NOT EXISTS {name}_embedding ON {table} USING hnsw (embedding {sp['ops']})")
    return changed


def ensure_library_indexes(c, dim=None):
    """The searchable layers on top of the passages: every source wiki version and every topic version cut into
    chunks with a vector and a word index, and a vector on every typed link between topics. -> True if any was converted to a new size."""
    sp = spec(dim or config.EMBEDDING_DIM)
    a = _chunk_table(c, 'library.wiki_chunk', 'source_id uuid NOT NULL,\n      version int NOT NULL',
                     'UNIQUE (source_id, version, idx),\n      FOREIGN KEY (source_id, version) REFERENCES library.wiki_version (source_id, version) ON DELETE CASCADE', sp)
    c.execute('CREATE INDEX IF NOT EXISTS wiki_chunk_scope ON library.wiki_chunk (collection_id, source_id, version, idx)')
    b = _chunk_table(c, 'library.topic_chunk', 'topic_id uuid NOT NULL,\n      version int NOT NULL',
                     'UNIQUE (topic_id, version, idx),\n      FOREIGN KEY (topic_id, version) REFERENCES library.topic_version (topic_id, version) ON DELETE CASCADE', sp)
    c.execute('CREATE INDEX IF NOT EXISTS topic_chunk_scope ON library.topic_chunk (collection_id, topic_id, version, idx)')
    d = ensure_embedding_column(c, 'library.topic_link', 'topic_link_embedding', dim)
    return a or b or d


def ensure_embedding_column(c, table, index, dim=None):
    """An `embedding` column of the configured size on a Library table (e.g. library.topic), with an HNSW index where pgvector allows one; converted (emptied) if the size changed."""
    dim = dim or config.EMBEDDING_DIM
    sp = spec(dim)
    schema, name = table.split('.')
    have = c.execute("SELECT format_type(a.atttypid, a.atttypmod) AS t FROM pg_attribute a WHERE a.attrelid=%s::regclass AND a.attname='embedding' AND NOT a.attisdropped", (table,)).fetchone()
    if have is None:
        c.execute(f"ALTER TABLE {table} ADD COLUMN embedding {sp['type']}")
        changed = False
    else:
        changed = have['t'] != sp['type']
        if changed:
            c.execute(f'DROP INDEX IF EXISTS {schema}.{index}')
            c.execute(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE {sp['type']} USING NULL")
    if sp['ops']:
        c.execute(f"CREATE INDEX IF NOT EXISTS {index} ON {table} USING hnsw (embedding {sp['ops']})")
    return changed
