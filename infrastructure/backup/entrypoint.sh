#!/bin/sh
# Dispatcher for the backup image, so both documented forms work:
#   ... run --rm backup [scheduled|predeploy|manual]           -> llmr-backup <label>
#   ... run --rm backup llmr-restore-verify <file>.dump.age    -> restore drill
#   ... run --rm backup llmr-backup <label>                    -> explicit form
set -eu
case "${1:-}" in
  llmr-backup|llmr-restore-verify)
    cmd="$1"; shift
    exec "$cmd" "$@" ;;
  *)
    exec llmr-backup "$@" ;;
esac
