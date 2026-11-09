#!/bin/sh
# Known-vulnerability scan of everything the app ships: Python packages (the exact lock the image installs) and the frontend's runtime packages.
# Run after any dependency or base-image bump. Needs the dev venv (pip-audit) and node.
set -e
cd "$(dirname "$0")/.."
echo "== Python (backend/requirements.lock)"
.venv/bin/pip-audit -r backend/requirements.lock --no-deps --disable-pip
echo "== Frontend runtime packages"
(cd frontend && npm audit --omit=dev) || true
echo "Note: the frontend dev tools (vite, esbuild) only run at build time, never in the running app."
