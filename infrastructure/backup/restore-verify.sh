#!/bin/sh
# Restore drill: decrypt a backup and restore it into a NEW scratch database, check it, then drop it.
# It never touches the live database (refuses to restore into PGDATABASE).
#
#   docker compose -f docker-compose.prod.yml --env-file <deploy.env> --profile tools run --rm \
#     -v <dir-with-age-identity>:/run/age:ro backup llmr-restore-verify <file.dump.age>
#
# Environment: PGHOST/PGUSER/PGPASSWORD (admin), PGDATABASE (live DB, used only to refuse it as target),
#   AGE_IDENTITY_FILE (default /run/age/key.txt): the age PRIVATE key, mounted read-only for the drill only.
#   KEEP_SCRATCH=1 keeps the scratch database for inspection.
set -eu
umask 077
file="${1:?usage: llmr-restore-verify <backup.dump.age>}"
case "$file" in /*) ;; *) file="/backups/$file" ;; esac
identity="${AGE_IDENTITY_FILE:-/run/age/key.txt}"
[ -r "$identity" ] || { echo "restore: age identity not readable at $identity" >&2; exit 2; }
[ -r "$file" ] || { echo "restore: backup not found: $file" >&2; exit 2; }

if [ -r "$file.sha256" ]; then
  ( cd "$(dirname "$file")" && sha256sum -c "$(basename "$file").sha256" >/dev/null ) \
    || { echo "restore: checksum mismatch" >&2; exit 1; }
  echo "restore: checksum OK"
fi

scratch="restore_verify_$(date -u +%Y%m%d%H%M%S)"
[ "$scratch" != "${PGDATABASE:-}" ] || { echo "restore: refusing to restore into the live database" >&2; exit 2; }
plain="/tmp/restore.dump"
trap 'rm -f "$plain"' EXIT INT TERM

age --decrypt --identity "$identity" --output "$plain" "$file"
echo "restore: decrypted; creating scratch database $scratch"
createdb --no-password --maintenance-db=postgres "$scratch"
cleanup_db() { [ "${KEEP_SCRATCH:-0}" = "1" ] || dropdb --no-password --maintenance-db=postgres --if-exists "$scratch"; }
trap 'rm -f "$plain"; cleanup_db' EXIT INT TERM

# --no-owner/--no-privileges: the scratch copy is owned by the admin role (the app role is not involved).
pg_restore --no-password --dbname="$scratch" --no-owner --no-privileges --exit-on-error "$plain"

tables="$(psql --no-password -d "$scratch" -tAc "select count(*) from information_schema.tables where table_schema not in ('pg_catalog','information_schema')")"
schema="$(psql --no-password -d "$scratch" -tAc "select table_schema from information_schema.tables where table_name='schema_migrations' limit 1")"
versions="$(psql --no-password -d "$scratch" -tAc "select coalesce(string_agg(version::text, ',' order by version), '') from \"${schema:-public}\".schema_migrations" 2>/dev/null || echo '?')"
rows="$(psql --no-password -d "$scratch" -tAc "select count(*) from \"${schema:-public}\".request_log" 2>/dev/null || echo '?')"
echo "restore: OK - ${tables} tables, schema ${schema:-?}, migrations [${versions}], request_log rows ${rows}"
[ "${tables:-0}" -ge 1 ] || { echo "restore: no tables restored" >&2; exit 1; }
