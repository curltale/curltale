# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Before you start

- For anything larger than a small fix, open an issue first so we can agree on the approach.
- Security problems: see [SECURITY.md](SECURITY.md), not a public issue.

## Development setup

```sh
python -m venv .venv && .venv/bin/pip install --require-hashes --no-deps -r backend/requirements.lock && .venv/bin/pip install -r backend/requirements-dev.txt
docker run -d --name curltale-testdb -e POSTGRES_PASSWORD=dev -p 127.0.0.1:55432:5432 pgvector/pgvector:0.8.7-pg16-bookworm
export TEST_DATABASE_URL=postgresql://postgres:dev@127.0.0.1:55432/postgres
cd backend && ../.venv/bin/python -m pytest -q tests          # creates and drops its own throwaway databases
cd frontend && npm ci && npx vite build
```

The tests never call an AI model or the network: models and embeddings are faked where a test needs them.

## Pull requests

- Keep each pull request to one change, with tests for new behaviour. CI must pass (tests at two embedding sizes, the frontend build, dependency audits, the images).
- **Isolation is not negotiable**: anything that reads Library content takes a collection handle and filters on it. The isolation tests must stay green.
- Adding a dependency: record its licence in `docs/licences.md` in the same change. No GPL, AGPL, SSPL, BUSL or non-commercial licences.
- Update the README when behaviour a user or an operator can see changes.
- Write user-facing text plainly: short sentences, no jargon the reader would not use.

## Licence

By contributing you agree that your contribution is released under the project's MIT licence.
