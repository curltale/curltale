# Changelog

All notable changes are listed here. The project follows semantic versioning; before 1.0, minor versions may change behaviour.

## [Unreleased]

### Security
This release hardens several parts of the platform. Upgrading is recommended for every installation, and in particular for those that connect to their own database or run the browser service.
- **Data queries:** stricter checks on what a query may contain. Every column must be named, and only standard functions that compute from their arguments can be called. Add your own safe database functions with `SQL_ALLOWED_FUNCTIONS` in `.env`.
- **Masked columns:** tighter rules. A masked column joins only to another masked column, and ordering by one orders by its hash.
- **Request size:** requests to `/api` and `/mcp` have a size limit (`MAX_BODY_MB`, default 4; uploads and Excel models keep their own limits), and larger requests are accepted only from a signed-in caller or a valid token.
- **Secrets:** the app does not start with a missing, placeholder or short (under 32 characters) `SECRET_KEY`, nor with the placeholder passwords of `.env.example`.
- **Browser service:** a current browser (Playwright 1.64.1) that runs inside its own sandbox. The container needs the seccomp profile and settings of the new `docker-compose.fetcher.yml`: rebuild it with `--build`. On a host without user namespaces, set `FETCH_NO_SANDBOX=1`.

### Changed
- A query that joins a masked column to a readable column, or calls a database function that is not a standard one, is now refused with an explanation.

### Added
- **Show sources**, a fifth entitlement on tokens and users. Off, the document tools return the content without the trail: no `[s1]` citation markers, no `sources` list, no links into the app, no collection ids, claims as their exact quotes, and `library_topic_sources` is not offered. The tool descriptions follow, so an agent is not told about what it cannot get. On by default, so nothing changes for existing credentials; Alfred uses the signed-in user's switch. Set it on **Users & access**.

## [0.2.0] - 2026-11-09

First public release.

### Ontology and data
- Governed semantic model: entities, fields, calculated fields, relationships, named filters, checks, query examples, custom properties; drafts, review, versioned publishing with an audit trail and optional four-eyes rule.
- Start from the Excel template or generate a first draft from a database (PostgreSQL, MySQL, MariaDB, ClickHouse, SQLite, DuckDB).
- Model check against the data, synthetic demo database, read-only SQL console.
- Access control by sensitivity class (confidential, personal, restricted) for users and API tokens; personal columns hashed, restricted ones never returned; every statement parsed, checked and rebuilt under the caller's entitlements.

### Knowledge base
- Collections with grants, a public or internal usage tag, and switches for AI and agent access.
- Files, web pages (with an optional browser service) and expert interviews.
- Verified source wiki per document (written, checked claim by claim, amended, reviewed when doubtful), topics written from the full wikis, claims with verbatim quotes and their wiki version, typed relationships, surgical revision when sources change.
- Every layer chunked, embedded and searchable by meaning and words.
- Review queue for contradictions, probable duplicates, doubtful pages, model links and vocabulary proposals; health checks; collection export.
- Bridge from topics to the entities of the model.

### Agents
- MCP endpoint with sixteen read-only tools, self-explaining instructions, `next` hints and ready prompts; Alfred, the built-in assistant.
- Agents read less for the same answer: compact tool results, short source references, leaner tool definitions, and prompt caching for the assistant and for the source checker.
- MCP instructions teach agents to read a question in three steps (understand the words, route to documents, data or both, answer with evidence); every tool description stands on its own and every result carries a `next` hint.

### Interface
- A web app with the brand typeface and the logo's teal accent, in light and dark themes.

### Operations
- Docker images (non-root, health checks), optional worker, browser service and S3 storage overlays, any OpenAI-compatible AI endpoint, local embeddings by default, activity log with tokens and cost.
