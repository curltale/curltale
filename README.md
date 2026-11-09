<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="frontend/public/curltale-logo-light.png"><img src="frontend/public/curltale-logo-dark.png" alt="Curltale AI" height="120"></picture></p>

# Curltale AI

**A hippocampus for AI agents: it grounds them in an ontology of your business data and a knowledge base of verified facts, so they know what your data means and what is true.**

The hippocampus is the part of the brain that turns experience into lasting, connected memory, and it is named after the seahorse. Curltale does that job for what your organisation knows: it takes your data and your documents and curls them into knowledge an agent can rely on, with proof.

It has two halves that share one login, one settings page, one activity log, one MCP endpoint and one built-in assistant, **Alfred**:

- **The ontology** is a governed model of your data: entities, fields, relationships, and the words people use ("open", "active", "unpaid") as exact conditions. It is versioned, reviewed and published like code, and it turns a question in plain language into one correct, read-only SQL statement on your own database, under the same access rules every time.
- **The knowledge library** is what people know about the business, kept in documents, web pages and heads. Files, pages and interviews become cited, reviewable topic pages in isolated **collections**, each with its own access. Every fact carries its exact quote and the source it came from. Topics can point at model entities ("this policy governs Invoice"), but the model never contains document content.

Agents reach both through an MCP server with sixteen read-only tools. People reach both through the app and Alfred.

It runs on your infrastructure: two containers to start (the app and PostgreSQL), plus optional ones when you need them. Your data stays in your database; Curltale reads it read-only, on demand. Open source, MIT.

## Documentation

Everything is at **[kb.curltale.com](https://kb.curltale.com)**. Good places to start:

| You want to... | Read |
|---|---|
| Get from nothing to a working system | [Your first hour](https://kb.curltale.com/start/first-hour/) |
| Install it | [Install](https://kb.curltale.com/start/install/) |
| Describe your data | [The model](https://kb.curltale.com/ontology/model/), [the Excel template](https://kb.curltale.com/ontology/excel/), [connectors](https://kb.curltale.com/ontology/connectors/) |
| Add documents and knowledge | [The knowledge library](https://kb.curltale.com/library/overview/) |
| Connect an agent | [Connect an agent](https://kb.curltale.com/agents/connect/), [the tools](https://kb.curltale.com/agents/tools/) |
| Manage users, access and settings | [Users, roles and access](https://kb.curltale.com/admin/users/), [switches and settings](https://kb.curltale.com/admin/settings/) |
| Fix a problem | [Troubleshooting](https://kb.curltale.com/help/troubleshooting/) |
| Understand it as a decision maker | [Curltale AI in plain words](https://kb.curltale.com/business/) |

## Quick start

You need Docker with Compose v2 and about 2 GB of memory for the app (the embedding model runs inside it). An AI endpoint (an OpenRouter key, or any OpenAI-compatible server) is optional, but Alfred and most of the library's writing and checking need it.

```sh
cp docker-compose.example.yml docker-compose.yml
cp .env.example .env          # fill in the REQUIRED values (openssl rand -hex 32 for secrets)
docker compose up -d
```

Open `http://localhost:8080` and sign in with `INITIAL_ADMIN_USERNAME` and `INITIAL_ADMIN_PASSWORD`. Then follow [Your first hour](https://kb.curltale.com/start/first-hour/): connect an AI endpoint, describe your data (a [sample workbook](https://kb.curltale.com/downloads/curltale-sample-model.xlsx) is there if you have none), publish, and ask Alfred a question.

Connect an agent with a token from **Users & access > Tokens & MCP**, for example with Claude Code:

```sh
claude mcp add --transport http curltale https://<your-host>/mcp --header "Authorization: Bearer <token>"
```

## What is in this repository

| Path | What it is |
|---|---|
| `docker-compose.example.yml` | The two core services: `db` (PostgreSQL 16 with pgvector) and `app`. |
| `docker-compose.worker.yml`, `docker-compose.fetcher.yml`, `docker-compose.storage.yml`, `docker-compose.demo.yml` | Optional services, added with `-f`: a worker, a browser service for JavaScript pages, an S3 bucket, demo databases. See [Optional services](https://kb.curltale.com/admin/optional/). |
| `.env.example` | Every setting, marked required or optional. The [settings reference](https://kb.curltale.com/admin/settings/) explains each. |
| `backend/` | The FastAPI app (`backend/studio/`) and its tests. |
| `frontend/` | The React UI. |
| `db/init/` | Runs once on a new database: creates the app's own non-superuser account and the vector extension. |
| `docs/` | Design notes (`design.md`) and third-party licences (`licences.md`). |
| `scripts/audit.sh` | Vulnerability scan of the locked Python packages and the frontend's runtime packages. |

## Security

The app runs as a non-root user with its own non-superuser database account. Every data query is parsed, checked against the caller's rights and rebuilt by the server before it runs; restricted columns never leave the database without an explicit grant, and personal ones are hashed. Library access is decided collection by collection. The full model is in [Operations and security](https://kb.curltale.com/admin/operations/). To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Development

```sh
python -m venv .venv && .venv/bin/pip install --require-hashes --no-deps -r backend/requirements.lock && .venv/bin/pip install -r backend/requirements-dev.txt
docker run -d --name curltale-testdb -e POSTGRES_PASSWORD=dev -p 127.0.0.1:55432:5432 pgvector/pgvector:0.8.7-pg16-bookworm
export TEST_DATABASE_URL=postgresql://postgres:dev@127.0.0.1:55432/postgres
cd backend && ../.venv/bin/python -m pytest -q tests      # creates and drops its own databases; never calls an AI model
cd frontend && npm ci && npx vite build                   # the UI
```

CI runs the suite at two embedding sizes, builds the UI and both images, and audits the dependencies. See [CONTRIBUTING.md](CONTRIBUTING.md) and [Development](https://kb.curltale.com/reference/development/) in the documentation.

## Why Curltale?

Seahorses curl their tails around something steady so the current cannot carry them away. The hippocampus, which turns moments into memory, is named after them. Agents drift without something steady to hold on to. Curltale is the tail: it holds on to your ontology and your verified facts, and curls your data and documents into a tale an agent can tell again, with proof.

## Licence

MIT. See [LICENSE](LICENSE).
