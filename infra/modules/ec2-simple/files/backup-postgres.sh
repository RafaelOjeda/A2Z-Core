#!/bin/bash
# Nightly Postgres backup -> S3 (non-negotiable: no RDS safety net on the
# single-box MVP -- docs/architecture/single-box-mvp.md,
# app/services/omnichannel/CLAUDE.md §12). Reuses the ledger bucket's
# existing IAM grant (s3:PutObject, plus s3:GetObject/ListBucket for
# restores -- infra/modules/iam) rather than needing a new one.
#
# NOTE: the ledger bucket's lifecycle rule (infra/modules/s3) is tuned for
# invoice PDFs/attachments, not backups specifically, and has no prefix
# filter -- it also governs backups/postgres/ (Glacier at 60d, deleted at
# 90d). See DEPLOYMENT.md's backup runbook for the usable window this
# creates. This script does no local retention of its own beyond the
# /tmp cleanup below; whatever expires the S3 objects is that lifecycle
# rule, not this script.
#
# Config comes entirely from environment variables (sourced from
# /opt/a2z-core/backup.env on the box; see user-data.sh) rather than
# Terraform template substitution, so this script can be exercised
# directly by tests/integration/backup/test_restore_drill.py without any
# templating step:
#   PGUSER       -- Postgres user (default: a2z)
#   PGDATABASE   -- Postgres database to dump (default: a2z)
#   COMPOSE_FILE -- docker-compose file the box's postgres service lives in
#   PG_EXEC      -- command prefix to run pg_dump with. Defaults to
#                   `docker compose -f "$COMPOSE_FILE" exec -T postgres`
#                   (the box's real path -- Postgres runs in a sibling
#                   container, not on the host). Tests set this to "" to
#                   run the local pg_dump binary directly against a
#                   throwaway database instead.
#   S3_BUCKET    -- destination bucket. If empty, the upload step is
#                   skipped entirely (used by the test to exercise
#                   dump/restore without AWS).
#   BACKUP_DIR   -- local directory for the dump file before upload
#                   (default: /tmp).
set -euo pipefail

PGUSER="${PGUSER:-a2z}"
PGDATABASE="${PGDATABASE:-a2z}"
COMPOSE_FILE="${COMPOSE_FILE:-/opt/a2z-core/docker-compose.yml}"
BACKUP_DIR="${BACKUP_DIR:-/tmp}"

# shellcheck disable=SC2206 # deliberate word-splitting: PG_EXEC is a
# command prefix (e.g. "docker compose -f x exec -T postgres"), not a
# single token.
if [ -z "${PG_EXEC+set}" ]; then
  PG_EXEC_ARR=(docker compose -f "$COMPOSE_FILE" exec -T postgres)
elif [ -z "$PG_EXEC" ]; then
  PG_EXEC_ARR=()
else
  read -ra PG_EXEC_ARR <<< "$PG_EXEC"
fi

STAMP="$(date +%F-%H%M)"
FILE="$BACKUP_DIR/a2z-postgres-$STAMP.sql.gz"

${PG_EXEC_ARR[@]+"${PG_EXEC_ARR[@]}"} pg_dump -U "$PGUSER" "$PGDATABASE" | gzip > "$FILE"

if [ -n "${S3_BUCKET:-}" ]; then
  aws s3 cp "$FILE" "s3://$S3_BUCKET/backups/postgres/$STAMP.sql.gz"
  rm -f "$FILE"
  echo "Backup uploaded: s3://$S3_BUCKET/backups/postgres/$STAMP.sql.gz"
else
  echo "S3_BUCKET not set -- backup left at $FILE (upload skipped)"
fi
