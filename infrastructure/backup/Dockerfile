# syntax=docker/dockerfile:1
# Backup/restore tooling: the same PostgreSQL major as the server (pg_dump/pg_restore 16) + age for encryption.
#   docker build -f infrastructure/backup/Dockerfile -t llm-router-backup:<git-sha> .   (context = repository root)
FROM postgres:16.15-alpine
RUN apk add --no-cache age
COPY infrastructure/backup/backup.sh /usr/local/bin/llmr-backup
COPY infrastructure/backup/restore-verify.sh /usr/local/bin/llmr-restore-verify
COPY infrastructure/backup/entrypoint.sh /usr/local/bin/llmr-entry
RUN chmod 0555 /usr/local/bin/llmr-backup /usr/local/bin/llmr-restore-verify /usr/local/bin/llmr-entry
# Run as the image's unprivileged postgres user (uid 70); never as root.
USER 70:70
# `run --rm backup predeploy` -> llmr-backup predeploy; `run --rm backup llmr-restore-verify <file>` -> drill.
ENTRYPOINT ["llmr-entry"]
CMD []
