#!/bin/sh
# Runs once, when the database is created (PostgreSQL entrypoint). The app gets its own account: owner of its database and schema,
# allowed to create the demo database and its roles, never a superuser. The superuser (POSTGRES_USER) stays for setup.
set -eu
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD is not set}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -v app_pw="$APP_DB_PASSWORD" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE ROLE studio_app LOGIN PASSWORD :'app_pw' NOSUPERUSER CREATEDB CREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER DATABASE studio OWNER TO studio_app;
ALTER SCHEMA public OWNER TO studio_app;
GRANT pg_signal_backend TO studio_app;
SQL
