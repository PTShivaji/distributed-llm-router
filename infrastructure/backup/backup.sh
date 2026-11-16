#!/bin/sh
# Encrypted PostgreSQL backup (custom-format pg_dump -> verify -> age encryption -> retention).
#
#   docker compose -f docker-compose.prod.yml --env-file <deploy.env> --profile tools run --rm backup [label]
#     label: "scheduled" (default) or "predeploy" (REQUIRED before every deployment; kept longer)
#
# Environment (from docker-compose.prod.yml):
#   PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE   admin connection (internal network only)
#   AGE_RECIPIENT                                    age PUBLIC key (age1...). The private key is NOT on the server.
#   BACKUP_RETENTION_DAYS (14)  PREDEPLOY_RETENTION_DAYS (30)  BACKUP_MIN_KEEP (7)
#
# The plaintext dump only ever exists in /tmp, a memory-backed tmpfs, and is deleted on exit.
# Off-site upload is a separate, explicit step (see DEPLOYMENT.md); this script never contacts external services.
set -eu
umask 077

label="${1:-scheduled}"
case "$label" in scheduled|predeploy|manual) ;; *) echo "backup: label must be scheduled|predeploy|manual" >&2; exit 2 ;; esac
: "${PGHOST:?}" "${PGUSER:?}" "${PGPASSWORD:?}" "${PGDATABASE:?}"
: "${AGE_RECIPIENT:?AGE_RECIPIENT (age public key) must be set}"
case "$AGE_RECIPIENT" in age1*) ;; *) echo "backup: AGE_RECIPIENT must be an age public key (age1...)" >&2; exit 2 ;; esac

out_dir=/backups
ts="$(date -u +%Y%m%dT%H%M%SZ)"
name="${PGDATABASE}-${ts}-${label}.dump"
plain="/tmp/${name}"
trap 'rm -f "$plain"' EXIT INT TERM

echo "backup: dumping ${PGDATABASE} (custom format)"
pg_dump --format=custom --compress=6 --no-password --file="$plain"

# Verify BEFORE encrypting: the archive must be readable and contain the application tables.
toc_entries="$(pg_restore --list "$plain" | grep -c ' TABLE ' || true)"
if [ "${toc_entries:-0}" -lt 1 ]; then
  echo "backup: verification failed (no tables in the archive)" >&2
  exit 1
fi
echo "backup: verified archive (${toc_entries} table entries)"

age --encrypt --recipient "$AGE_RECIPIENT" --output "${out_dir}/${name}.age.partial" "$plain"
mv "${out_dir}/${name}.age.partial" "${out_dir}/${name}.age"
( cd "$out_dir" && sha256sum "${name}.age" > "${name}.age.sha256" )
echo "backup: wrote ${name}.age ($(wc -c < "${out_dir}/${name}.age") bytes) + .sha256"

# Retention: delete old encrypted backups, but always keep the newest BACKUP_MIN_KEEP of each label.
prune() {
  lbl="$1"; days="$2"; keep="${BACKUP_MIN_KEEP:-7}"
  ls -1t "$out_dir"/*-"$lbl".dump.age 2>/dev/null | tail -n +"$((keep + 1))" | while read -r f; do
    if [ -n "$(find "$f" -mtime +"$days" 2>/dev/null)" ]; then
      rm -f "$f" "$f.sha256" && echo "backup: pruned $(basename "$f")"
    fi
  done
}
prune scheduled "${BACKUP_RETENTION_DAYS:-14}"
prune predeploy "${PREDEPLOY_RETENTION_DAYS:-30}"
prune manual "${PREDEPLOY_RETENTION_DAYS:-30}"
echo "backup: done"
