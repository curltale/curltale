-- The Library lives in its own schema; every table that holds document-derived data carries collection_id.
CREATE SCHEMA IF NOT EXISTS library;

CREATE TABLE IF NOT EXISTS library.collection (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
  name        text NOT NULL,
  description text NOT NULL DEFAULT '',
  usage       text NOT NULL DEFAULT 'internal' CHECK (usage IN ('public', 'internal')),   -- advice returned with every result, not an access rule
  created_at  timestamptz NOT NULL DEFAULT now(),
  created_by  text
);

-- who may do what in a collection: a user or an API token, never both
CREATE TABLE IF NOT EXISTS library.grant_ (
  id            bigserial PRIMARY KEY,
  collection_id uuid NOT NULL REFERENCES library.collection ON DELETE CASCADE,
  username      text REFERENCES public.app_user ON DELETE CASCADE,
  token_id      int  REFERENCES public.api_token ON DELETE CASCADE,
  level         text NOT NULL CHECK (level IN ('read', 'write', 'manage')),
  granted_by    text,
  granted_at    timestamptz NOT NULL DEFAULT now(),
  CHECK ((username IS NULL) <> (token_id IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS grant_user ON library.grant_ (collection_id, username) WHERE username IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS grant_token ON library.grant_ (collection_id, token_id) WHERE token_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS grant_subject ON library.grant_ (username);

-- original files
CREATE TABLE IF NOT EXISTS library.source (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  collection_id uuid NOT NULL REFERENCES library.collection ON DELETE CASCADE,
  name          text NOT NULL,
  kind          text NOT NULL DEFAULT 'file',
  mime          text,
  size          bigint NOT NULL DEFAULT 0,
  sha256        text NOT NULL,
  storage_key   text NOT NULL,
  status        text NOT NULL DEFAULT 'queued',          -- queued, processing, ready, failed (more stages arrive with the pipeline)
  error         text,
  created_by    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (collection_id, sha256)
);
CREATE INDEX IF NOT EXISTS source_collection ON library.source (collection_id, created_at DESC);

-- background work: a queue in the database. Payloads hold ids only, never document text.
CREATE TABLE IF NOT EXISTS library.job (
  id            bigserial PRIMARY KEY,
  kind          text NOT NULL,
  collection_id uuid NOT NULL REFERENCES library.collection ON DELETE CASCADE,
  source_id     uuid REFERENCES library.source ON DELETE CASCADE,
  payload       jsonb NOT NULL DEFAULT '{}',
  status        text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
  attempts      int NOT NULL DEFAULT 0,
  max_attempts  int NOT NULL DEFAULT 3,
  run_after     timestamptz NOT NULL DEFAULT now(),
  lease_until   timestamptz,
  worker        text,
  progress      jsonb NOT NULL DEFAULT '{}',
  error         text,
  created_by    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  started_at    timestamptz,
  finished_at   timestamptz
);
CREATE INDEX IF NOT EXISTS job_queue ON library.job (run_after) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS job_source ON library.job (source_id, id DESC);

ALTER TABLE library.source ADD COLUMN IF NOT EXISTS title text;
ALTER TABLE library.source ADD COLUMN IF NOT EXISTS stats jsonb NOT NULL DEFAULT '{}';
CREATE UNIQUE INDEX IF NOT EXISTS source_id_collection ON library.source (id, collection_id);   -- lets children point at (source, collection) together, so a row can never name another collection's source

-- the text read from a file
CREATE TABLE IF NOT EXISTS library.source_text (
  source_id     uuid PRIMARY KEY,
  collection_id uuid NOT NULL,
  title         text,
  text          text NOT NULL,
  extractor     text,
  FOREIGN KEY (source_id, collection_id) REFERENCES library.source (id, collection_id) ON DELETE CASCADE
);
-- library.source_chunk is created by vectors.ensure_library_chunks (its vector column depends on the embedding size)

-- what the AI may do with a collection, and whether agents may read it at all
ALTER TABLE library.collection ADD COLUMN IF NOT EXISTS allow_ai boolean NOT NULL DEFAULT true;           -- false: its text is never sent to a model (no wiki, no OCR); search still works
ALTER TABLE library.collection ADD COLUMN IF NOT EXISTS agents_may_read boolean NOT NULL DEFAULT true;    -- false: only people read it; MCP and Alfred never see it

-- the source wiki: one current page per source, every version kept, with the checks that led to it
ALTER TABLE library.source ADD COLUMN IF NOT EXISTS wiki_status text NOT NULL DEFAULT 'none';   -- none, skipped, approved, needs_review, failed
CREATE TABLE IF NOT EXISTS library.wiki_version (
  id            bigserial PRIMARY KEY,
  collection_id uuid NOT NULL,
  source_id     uuid NOT NULL,
  version       int NOT NULL,
  markdown      text NOT NULL,
  origin        text NOT NULL,                      -- written, amended, edited
  created_by    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source_id, version),
  FOREIGN KEY (source_id, collection_id) REFERENCES library.source (id, collection_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS library.wiki_check (
  id            bigserial PRIMARY KEY,
  collection_id uuid NOT NULL,
  source_id     uuid NOT NULL,
  version       int NOT NULL,
  round         int NOT NULL,
  verdict       text NOT NULL,                      -- PASS, FAIL, ERROR
  issues        jsonb NOT NULL DEFAULT '[]',
  tool_calls    int NOT NULL DEFAULT 0,
  at            timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (source_id, collection_id) REFERENCES library.source (id, collection_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS wiki_check_source ON library.wiki_check (source_id, version, round);

ALTER TABLE library.source ADD COLUMN IF NOT EXISTS url text;                 -- set for sources captured from a web address

-- ============================================================ topics: the wiki of a collection
-- The vocabulary (what kinds of topic and relation exist) belongs to the collection, seeded from a starter set; nothing here is shared between collections.
CREATE TABLE IF NOT EXISTS library.topic_type (
  collection_id uuid NOT NULL REFERENCES library.collection ON DELETE CASCADE,
  name          text NOT NULL,
  description   text NOT NULL DEFAULT '',
  status        text NOT NULL DEFAULT 'approved' CHECK (status IN ('approved', 'pending', 'rejected')),
  PRIMARY KEY (collection_id, name)
);
CREATE TABLE IF NOT EXISTS library.relation_type (
  collection_id uuid NOT NULL REFERENCES library.collection ON DELETE CASCADE,
  name          text NOT NULL,
  description   text NOT NULL DEFAULT '',
  inverse_name  text NOT NULL DEFAULT '',
  status        text NOT NULL DEFAULT 'approved' CHECK (status IN ('approved', 'pending', 'rejected')),
  PRIMARY KEY (collection_id, name)
);
-- the analyzer proposes a new kind when nothing fits; the collection's manager decides
CREATE TABLE IF NOT EXISTS library.vocab_proposal (
  id            bigserial PRIMARY KEY,
  collection_id uuid NOT NULL REFERENCES library.collection ON DELETE CASCADE,
  kind          text NOT NULL CHECK (kind IN ('topic_type', 'relation_type')),
  name          text NOT NULL,
  description   text NOT NULL DEFAULT '',
  reason        text NOT NULL DEFAULT '',
  source_id     uuid,
  status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (collection_id, kind, name)
);

CREATE TABLE IF NOT EXISTS library.topic (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  collection_id  uuid NOT NULL REFERENCES library.collection ON DELETE CASCADE,
  slug           text NOT NULL,
  title          text NOT NULL,
  topic_type     text NOT NULL DEFAULT 'Topic',
  summary        text NOT NULL DEFAULT '',
  status         text NOT NULL DEFAULT 'active',
  needs_revision boolean NOT NULL DEFAULT false,         -- a source it relied on was removed or replaced: the page has to be revised
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (collection_id, slug),
  UNIQUE (id, collection_id)
);
-- library.topic.embedding (of title, aliases and summary) is added by vectors.ensure_library_topic_vectors

CREATE TABLE IF NOT EXISTS library.topic_version (
  id            bigserial PRIMARY KEY,
  collection_id uuid NOT NULL,
  topic_id      uuid NOT NULL,
  version       int NOT NULL,
  markdown      text NOT NULL,
  update_reason text NOT NULL DEFAULT '',
  status        text NOT NULL DEFAULT 'published' CHECK (status IN ('published', 'held', 'rejected')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (topic_id, version),
  FOREIGN KEY (topic_id, collection_id) REFERENCES library.topic (id, collection_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS library.topic_alias (
  collection_id uuid NOT NULL,
  topic_id      uuid NOT NULL,
  alias         text NOT NULL,
  PRIMARY KEY (collection_id, alias),
  FOREIGN KEY (topic_id, collection_id) REFERENCES library.topic (id, collection_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS library.topic_link (
  collection_id uuid NOT NULL,
  from_topic    uuid NOT NULL,
  to_topic      uuid NOT NULL,
  relation      text NOT NULL DEFAULT 'related_to',
  context       text NOT NULL DEFAULT '',
  PRIMARY KEY (from_topic, to_topic, relation),
  FOREIGN KEY (from_topic, collection_id) REFERENCES library.topic (id, collection_id) ON DELETE CASCADE,
  FOREIGN KEY (to_topic, collection_id) REFERENCES library.topic (id, collection_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS library.topic_source (
  collection_id uuid NOT NULL,
  topic_id      uuid NOT NULL,
  source_id     uuid NOT NULL,
  PRIMARY KEY (topic_id, source_id),
  FOREIGN KEY (topic_id, collection_id) REFERENCES library.topic (id, collection_id) ON DELETE CASCADE,
  FOREIGN KEY (source_id, collection_id) REFERENCES library.source (id, collection_id) ON DELETE CASCADE
);

-- facts, each tied to the quotes that support it; the page cites them as [c<n>]
CREATE TABLE IF NOT EXISTS library.claim (
  id            bigserial PRIMARY KEY,
  collection_id uuid NOT NULL,
  topic_id      uuid NOT NULL,
  n             int NOT NULL,                              -- the number the page cites: [c<n>]
  text          text NOT NULL,
  kind          text NOT NULL DEFAULT 'fact',
  status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'pending', 'superseded', 'rejected')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (topic_id, n),
  FOREIGN KEY (topic_id, collection_id) REFERENCES library.topic (id, collection_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS library.claim_source (
  id            bigserial PRIMARY KEY,
  collection_id uuid NOT NULL,
  claim_id      bigint NOT NULL REFERENCES library.claim ON DELETE CASCADE,
  source_id     uuid NOT NULL,
  quote         text NOT NULL,                             -- exact text from the source page
  passage_idx   int,                                       -- the passage of the original document that best supports it
  location      text,
  FOREIGN KEY (source_id, collection_id) REFERENCES library.source (id, collection_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS claim_source_claim ON library.claim_source (claim_id);
CREATE INDEX IF NOT EXISTS claim_source_source ON library.claim_source (source_id);

-- ============================================================ the review gate and housekeeping of the topic wiki
-- topic.status: active, draft (new and held back as a probable duplicate), archived (nothing supports it any more)
-- topic_version.status 'held': written but not published until a person decides
CREATE TABLE IF NOT EXISTS library.topic_conflict (
  id             bigserial PRIMARY KEY,
  collection_id  uuid NOT NULL,
  topic_id       uuid NOT NULL,
  version        int NOT NULL,                                -- the held version
  base_version   int,                                         -- the published version it was written from
  kind           text NOT NULL CHECK (kind IN ('contradiction', 'duplicate_topic')),
  description    text NOT NULL,
  contradicts_n  int,                                         -- claim number on the topic that the new evidence contradicts
  duplicate_of   uuid,
  source_id      uuid,
  status         text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'approved', 'rejected', 'redirected')),
  decided_by     text,
  decided_at     timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (topic_id, collection_id) REFERENCES library.topic (id, collection_id) ON DELETE CASCADE,
  FOREIGN KEY (duplicate_of, collection_id) REFERENCES library.topic (id, collection_id) ON DELETE SET NULL (duplicate_of),
  FOREIGN KEY (source_id, collection_id) REFERENCES library.source (id, collection_id) ON DELETE SET NULL (source_id)
);
CREATE INDEX IF NOT EXISTS topic_conflict_open ON library.topic_conflict (collection_id, status);

-- a merged topic's old address still leads to the topic that took it over
CREATE TABLE IF NOT EXISTS library.topic_redirect (
  collection_id uuid NOT NULL,
  from_slug     text NOT NULL,
  to_topic      uuid NOT NULL,
  reason        text NOT NULL DEFAULT '',
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (collection_id, from_slug),
  FOREIGN KEY (to_topic, collection_id) REFERENCES library.topic (id, collection_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS library.health_run (
  id            bigserial PRIMARY KEY,
  collection_id uuid NOT NULL REFERENCES library.collection ON DELETE CASCADE,
  at            timestamptz NOT NULL DEFAULT now(),
  by            text,
  findings      jsonb NOT NULL DEFAULT '[]'
);

-- statements are searched by words and (embedding column added by vectors.ensure_embedding_column) by meaning
ALTER TABLE library.claim ADD COLUMN IF NOT EXISTS tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED;
CREATE INDEX IF NOT EXISTS claim_tsv ON library.claim USING gin (tsv);

-- ============================================================ the bridge to the governed model
-- A topic points at model objects (entities, fields). The pointer lives here, in the collection: the model never learns that a collection exists, and
-- whoever cannot read the collection never sees the link. `ref` is the entity key, or 'entity.column' for a field; it is resolved against the
-- published model whenever it is read.
CREATE TABLE IF NOT EXISTS library.topic_model_ref (
  collection_id uuid NOT NULL,
  topic_id      uuid NOT NULL,
  kind          text NOT NULL CHECK (kind IN ('entity', 'field')),
  ref           text NOT NULL,
  relation      text NOT NULL DEFAULT 'about' CHECK (relation IN ('instance_of', 'about', 'governs', 'uses')),
  status        text NOT NULL DEFAULT 'confirmed' CHECK (status IN ('confirmed', 'suggested', 'rejected')),
  origin        text NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual', 'ai')),
  confidence    real,
  reason        text NOT NULL DEFAULT '',
  created_by    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (topic_id, kind, ref),
  FOREIGN KEY (topic_id, collection_id) REFERENCES library.topic (id, collection_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS topic_model_ref_lookup ON library.topic_model_ref (kind, ref) WHERE status = 'confirmed';

-- ============================================================ interviews: capturing what a person knows by talking it through
CREATE TABLE IF NOT EXISTS library.interview (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  collection_id uuid NOT NULL REFERENCES library.collection ON DELETE CASCADE,
  seed          text NOT NULL,                          -- what the person wants to document, in their words
  status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'draft_ready', 'ingesting', 'ingested')),
  created_by    text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  source_id     uuid,
  UNIQUE (id, collection_id),
  FOREIGN KEY (source_id, collection_id) REFERENCES library.source (id, collection_id) ON DELETE SET NULL (source_id)
);
CREATE TABLE IF NOT EXISTS library.interview_turn (
  id            bigserial PRIMARY KEY,
  collection_id uuid NOT NULL,
  interview_id  uuid NOT NULL,
  role          text NOT NULL CHECK (role IN ('user', 'agent')),
  content       text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (interview_id, collection_id) REFERENCES library.interview (id, collection_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS interview_turn_order ON library.interview_turn (interview_id, id);
-- the facts established so far: the interview's long-term memory
CREATE TABLE IF NOT EXISTS library.interview_claim (
  id            bigserial PRIMARY KEY,
  collection_id uuid NOT NULL,
  interview_id  uuid NOT NULL,
  text          text NOT NULL,
  category      text,
  status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'removed')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (interview_id, collection_id) REFERENCES library.interview (id, collection_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS library.interview_draft (
  id            bigserial PRIMARY KEY,
  collection_id uuid NOT NULL,
  interview_id  uuid NOT NULL,
  version       int NOT NULL,
  markdown      text NOT NULL,
  origin        text NOT NULL DEFAULT 'written',
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (interview_id, version),
  FOREIGN KEY (interview_id, collection_id) REFERENCES library.interview (id, collection_id) ON DELETE CASCADE
);

-- ============================================================ every layer chunked, embedded and searchable
-- the topic types carry the page template the topic writer follows
ALTER TABLE library.topic_type ADD COLUMN IF NOT EXISTS template text NOT NULL DEFAULT '';
-- claims are superseded, never deleted, so a surgical rewrite can audit what a source used to support
ALTER TABLE library.claim ADD COLUMN IF NOT EXISTS superseded_at timestamptz;
-- a quote is taken from one version of the source wiki: which one, and in which of its passages it sits
ALTER TABLE library.claim_source ADD COLUMN IF NOT EXISTS wiki_version int;
ALTER TABLE library.claim_source ADD COLUMN IF NOT EXISTS wiki_chunk int;
ALTER TABLE library.topic_source ADD COLUMN IF NOT EXISTS wiki_version int;
-- library.wiki_chunk (every wiki version), library.topic_chunk (every topic version) and library.topic_link.embedding are created by vectors.ensure_library_indexes
-- when a source is deleted its quotes stay (source_id becomes NULL), so the lint pass can audit what the topic used to rest on
ALTER TABLE library.claim_source ALTER COLUMN source_id DROP NOT NULL;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='claim_source_source_id_collection_id_fkey' AND conrelid='library.claim_source'::regclass AND confdeltype='c') THEN
    ALTER TABLE library.claim_source DROP CONSTRAINT claim_source_source_id_collection_id_fkey;
    ALTER TABLE library.claim_source ADD CONSTRAINT claim_source_source_id_collection_id_fkey FOREIGN KEY (source_id, collection_id) REFERENCES library.source (id, collection_id) ON DELETE SET NULL (source_id);
  END IF;
END $$;
