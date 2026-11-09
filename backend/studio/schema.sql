CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS ontology_version (
  id            serial PRIMARY KEY,
  label         text NOT NULL,
  status        text NOT NULL CHECK (status IN ('draft','published','archived')),
  note          text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  published_at  timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS one_draft ON ontology_version ((status)) WHERE status = 'draft';
CREATE UNIQUE INDEX IF NOT EXISTS one_published ON ontology_version ((status)) WHERE status = 'published';

-- The model has four concepts: entities, fields (attributes and measures), relationships, examples. Each version is a full copy of them.
-- Columns for what code reads and users edit; `extra` keeps everything else from the source so nothing is lost.
CREATE TABLE IF NOT EXISTS entity (
  version_id        int  NOT NULL REFERENCES ontology_version ON DELETE CASCADE,
  key               text NOT NULL,
  position          int  NOT NULL DEFAULT 0,
  name              text NOT NULL,
  domain            text,
  kind              text,
  definition        text,
  semantic_table    text,
  primary_key       text,
  business_key      text,
  system_of_record  text,
  synonyms          jsonb NOT NULL DEFAULT '[]',      -- words people use for it (abbreviations, concepts like 'billing')
  filters           jsonb NOT NULL DEFAULT '[]',      -- named filters on its table: [{name, synonyms, filter, description, status, source}]
  checks            jsonb NOT NULL DEFAULT '[]',      -- data quality rules about it: [{id, rule, sql?, note?}]
  extra             json  NOT NULL DEFAULT '{}',      -- tmf, lifecycle_states, not_to_confuse_with, example_questions, custom, ...
  CONSTRAINT entity_v2_pk PRIMARY KEY (version_id, key)
);

CREATE TABLE IF NOT EXISTS field (
  version_id      int  NOT NULL,
  entity_key      text NOT NULL,
  col             text NOT NULL,
  position        int  NOT NULL DEFAULT 0,
  role            text,                                -- primary_key, foreign_key, attribute, dimension, fact_measure, lineage, ... or 'measure' (no physical column)
  type            text,
  business_name   text,
  description     text,
  sensitivity     text,
  agent_access    text,
  review_status   text,
  allowed_values  jsonb,                               -- ordered; replaces the separate code lists
  business_term   text,
  extra           json NOT NULL DEFAULT '{}',          -- references, lineage, tmf_name, example, value_labels, aggregation_rule (measures), ...
  CONSTRAINT field_v2_pk PRIMARY KEY (version_id, entity_key, col),
  CONSTRAINT field_v2_entity FOREIGN KEY (version_id, entity_key) REFERENCES entity (version_id, key) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS relationship (
  version_id   int  NOT NULL REFERENCES ontology_version ON DELETE CASCADE,
  rid          text NOT NULL,
  position     int  NOT NULL DEFAULT 0,
  from_entity  text NOT NULL,
  to_entity    text NOT NULL,
  predicate    text,
  cardinality  text,
  join_cond    text,
  status       text,
  extra        json NOT NULL DEFAULT '{}',
  CONSTRAINT relationship_v2_pk PRIMARY KEY (version_id, rid)
);

-- Rules, terms, processes and notes used to live here next to the examples. They belong to the document Library now: only query examples remain in the model.
DO $$
BEGIN
  IF to_regclass('public.knowledge') IS NOT NULL AND to_regclass('public.example') IS NULL THEN
    DELETE FROM knowledge WHERE type <> 'example';
    DELETE FROM chunk WHERE type = 'knowledge' AND NOT EXISTS (SELECT 1 FROM knowledge k WHERE k.version_id = chunk.version_id AND 'kb:' || k.id = chunk.id);
    UPDATE chunk SET type = 'example', id = 'ex:' || substr(id, 4) WHERE type = 'knowledge';
    ALTER TABLE knowledge DROP COLUMN type, DROP COLUMN pinned;
    ALTER TABLE knowledge RENAME TO example;
    ALTER TABLE example RENAME CONSTRAINT knowledge_v2_pk TO example_pk;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS example (
  version_id  int  NOT NULL REFERENCES ontology_version ON DELETE CASCADE,
  id          text NOT NULL,
  position    int  NOT NULL DEFAULT 0,
  title       text,                                    -- the question the example answers
  body        text,                                    -- a note on the path it takes
  applies_to  jsonb NOT NULL DEFAULT '[]',             -- entity keys it is about
  extra       json NOT NULL DEFAULT '{}',              -- sql, params, sample_sql, topic, source, ...
  CONSTRAINT example_pk PRIMARY KEY (version_id, id)
);

CREATE TABLE IF NOT EXISTS meta (
  version_id int  NOT NULL REFERENCES ontology_version ON DELETE CASCADE,
  key        text NOT NULL,                            -- ontology, access_policy
  data       json NOT NULL,
  CONSTRAINT meta_v2_pk PRIMARY KEY (version_id, key)
);

-- derived: rebuilt on publish, never edited
CREATE TABLE IF NOT EXISTS chunk (
  version_id    int  NOT NULL REFERENCES ontology_version ON DELETE CASCADE,
  id            text NOT NULL,
  type          text NOT NULL,
  title         text,
  text          text NOT NULL,
  entity        text,
  agent_access  text,
  sensitivity   text,
  review_status text,
  metadata      jsonb,
  content_hash  text NOT NULL,
  tsv           tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(title,'') || ' ' || text)) STORED,
  embedding     vector(__DIM__),
  PRIMARY KEY (version_id, id)
);
CREATE INDEX IF NOT EXISTS chunk_tsv  ON chunk USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunk_hash ON chunk (content_hash);
CREATE INDEX IF NOT EXISTS chunk_type ON chunk (version_id, type);

CREATE TABLE IF NOT EXISTS audit_log (
  id         bigserial PRIMARY KEY,
  at         timestamptz NOT NULL DEFAULT now(),
  username   text,
  version_id int,
  action     text NOT NULL,
  kind       text,
  key        text,
  before     json,
  after      json
);
CREATE INDEX IF NOT EXISTS audit_version ON audit_log (version_id, at DESC);

CREATE TABLE IF NOT EXISTS app_user (
  username      text PRIMARY KEY,
  password_hash text NOT NULL,
  role          text NOT NULL DEFAULT 'modeller' CHECK (role IN ('admin','modeller')),
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS session (
  token_hash text PRIMARY KEY,
  username   text NOT NULL REFERENCES app_user ON DELETE CASCADE,
  expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS api_token (
  id         serial PRIMARY KEY,
  name       text NOT NULL,
  token_hash text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_used  timestamptz
);

-- questions Alfred / MCP clients could not answer well: a backlog for the modellers
CREATE TABLE IF NOT EXISTS question_log (
  id         bigserial PRIMARY KEY,
  at         timestamptz NOT NULL DEFAULT now(),
  source     text,
  question   text NOT NULL,
  top_score  real,
  answered   boolean
);

-- entitlements travel with the credential (see access.py)
ALTER TABLE api_token ADD COLUMN IF NOT EXISTS entitlements jsonb NOT NULL DEFAULT '{"confidential": true, "personal": true, "restricted": false}';
ALTER TABLE app_user  ADD COLUMN IF NOT EXISTS entitlements jsonb NOT NULL DEFAULT '{"confidential": true, "personal": true, "restricted": false}';

-- builds of the synthetic database (semantic_data), one row per run
CREATE TABLE IF NOT EXISTS data_build (
  id          serial PRIMARY KEY,
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  status      text NOT NULL DEFAULT 'running' CHECK (status IN ('running','ok','failed')),
  scale       text,
  seed        int,
  version_id  int,
  message     text,
  stats       jsonb,
  findings    jsonb,
  checks      jsonb,
  started_by  text
);

-- activity: who did what. A session is one Alfred conversation, or one burst of MCP calls by a token (idle gap ends it). Events are the steps in it:
-- the question, each model call (tokens, cost), each tool call (time, outcome), the answer. Results are summarised, never stored; purged after ACTIVITY_DAYS.
CREATE TABLE IF NOT EXISTS activity_session (
  id         text PRIMARY KEY,
  source     text NOT NULL,
  actor      text NOT NULL,
  title      text,
  started_at timestamptz NOT NULL DEFAULT now(),
  last_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS activity_session_last ON activity_session (last_at DESC);
CREATE TABLE IF NOT EXISTS activity_event (
  id         bigserial PRIMARY KEY,
  session_id text NOT NULL REFERENCES activity_session ON DELETE CASCADE,
  at         timestamptz NOT NULL DEFAULT now(),
  kind       text NOT NULL,          -- user | llm | tool | answer | error
  name       text,                   -- tool name or model
  ms         integer,
  ok         boolean NOT NULL DEFAULT true,
  tokens_in  integer,
  tokens_out integer,
  cost       numeric(12,6),
  detail     jsonb
);
CREATE INDEX IF NOT EXISTS activity_event_session ON activity_event (session_id, id);
CREATE INDEX IF NOT EXISTS activity_event_at ON activity_event (at);

-- connectors: the database the model describes. One is active at a time (none active = the built-in synthetic demo database). Passwords are encrypted, never returned.
CREATE TABLE IF NOT EXISTS connector (
  id         serial PRIMARY KEY,
  name       text NOT NULL UNIQUE,
  kind       text NOT NULL DEFAULT 'postgres',
  params     jsonb NOT NULL,
  secret     text,
  options    jsonb NOT NULL DEFAULT '{}',
  active     boolean NOT NULL DEFAULT false,
  status     jsonb,
  catalog    jsonb,
  catalog_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS connector_one_active ON connector (active) WHERE active;

-- small app-wide flags (e.g. the starting model was imported once: an emptied ontology must stay empty after a restart)
CREATE TABLE IF NOT EXISTS app_setting (
  key   text PRIMARY KEY,
  value jsonb NOT NULL
);

-- embeddings by content: survive resets and deleted versions, so publishing text that was embedded before costs nothing
CREATE TABLE IF NOT EXISTS embedding_cache (
  content_hash text NOT NULL,
  model        text NOT NULL,
  embedding    vector(__DIM__) NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (content_hash, model)
);

-- login protection: every attempt is kept a day; 5 failures for a username (or 30 from one client) in 15 minutes block further tries for 15 minutes
CREATE TABLE IF NOT EXISTS login_attempt (
  id       bigserial PRIMARY KEY,
  at       timestamptz NOT NULL DEFAULT now(),
  username text,
  ip       text,
  ok       boolean NOT NULL
);
CREATE INDEX IF NOT EXISTS login_attempt_user ON login_attempt (username, at);
CREATE INDEX IF NOT EXISTS login_attempt_ip ON login_attempt (ip, at);
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS must_change boolean NOT NULL DEFAULT false;
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS password_changed_at timestamptz;
-- four roles (each includes the ones before): viewer < modeller < publisher < admin
ALTER TABLE app_user DROP CONSTRAINT IF EXISTS app_user_role_check;
ALTER TABLE app_user ADD CONSTRAINT app_user_role_check CHECK (role IN ('viewer', 'modeller', 'publisher', 'admin'));
-- roles outside the ladder (today: editor); the ladder itself stays in role
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS extra_roles text[] NOT NULL DEFAULT '{}';
