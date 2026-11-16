#!/bin/sh
# Fresh-volume initialisation for PRODUCTION (run once by the official postgres image entrypoint, only when
# /var/lib/postgresql/data is empty; it never runs against an existing database).
#
# Roles:
#   $POSTGRES_USER      bootstrap/admin role created by the image (superuser). Used ONLY for operations:
#                       backups, restores, maintenance. Never given to the API.
#   $POSTGRES_APP_USER  application role used by the API: LOGIN, NOT superuser, cannot create databases or
#                       roles, cannot bypass row security, no replication. It OWNS one dedicated schema
#                       ($POSTGRES_APP_SCHEMA, default llm_router) and nothing else, so the built-in migration
#                       runner (advisory lock + CREATE/ALTER of its own tables) works without extra privileges.
#   PUBLIC              loses CONNECT on the database and every privilege on schema public.
#
# The password is read from the environment with psql's \getenv, so it never appears in process arguments
# or in this file. The body runs in a subshell because the entrypoint *sources* non-executable .sh files.
(
  set -eu
  : "${POSTGRES_USER:?}" "${POSTGRES_DB:?}"
  : "${POSTGRES_APP_USER:?POSTGRES_APP_USER must be set}"
  : "${POSTGRES_APP_PASSWORD:?POSTGRES_APP_PASSWORD must be set}"
  APP_SCHEMA="${POSTGRES_APP_SCHEMA:-llm_router}"
  export APP_SCHEMA

  if [ "$POSTGRES_APP_USER" = "$POSTGRES_USER" ]; then
    echo "10-app-role.sh: POSTGRES_APP_USER must differ from the admin role" >&2
    exit 1
  fi
  if [ "${#POSTGRES_APP_PASSWORD}" -lt 16 ]; then
    echo "10-app-role.sh: POSTGRES_APP_PASSWORD must be at least 16 characters" >&2
    exit 1
  fi

  psql -v ON_ERROR_STOP=1 --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv app_user POSTGRES_APP_USER
\getenv app_password POSTGRES_APP_PASSWORD
\getenv app_schema APP_SCHEMA
\getenv db POSTGRES_DB

SET password_encryption = 'scram-sha-256';
CREATE ROLE :"app_user" WITH LOGIN PASSWORD :'app_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 50;

-- Database: only the app role (and the admin) may connect.
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"db" TO :"app_user";

-- No one but the owner may create or use objects in schema public.
REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- The application's own schema, owned by the app role (tables are created by its migrations).
CREATE SCHEMA :"app_schema" AUTHORIZATION :"app_user";
ALTER ROLE :"app_user" IN DATABASE :"db" SET search_path = :"app_schema";
-- Safety net: a session stuck inside an open transaction is ended (no statement_timeout: the migration
-- runner legitimately waits on an advisory lock while another instance migrates).
ALTER ROLE :"app_user" IN DATABASE :"db" SET idle_in_transaction_session_timeout = '60s';
SQL
  echo "10-app-role.sh: application role and schema created"
)
