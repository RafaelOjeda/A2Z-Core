#!/bin/bash
# Restore a pg_dump backup into a FRESH database -- never onto the live one.
#
# The dump is plain-format SQL (`pg_dump | gzip`, produced by
# backup-postgres.sh in this same directory), so it contains `CREATE
# SCHEMA` for omnichannel/invoicing but not `CREATE DATABASE`. Restoring it
# onto the box's live `a2z` database -- which already has both schemas --
# would collide on every object. Instead this script restores into a new
# database so the operator can inspect it, run any pending Alembic
# upgrades, and only then swap `DATABASE_URL` and restart the app. See
# DEPLOYMENT.md's backup runbook for the full drill, including the
# migration-skew step this script does not attempt to automate.
#
# Usage:
#   restore-postgres.sh <s3-key|local-path-to-.sql[.gz]> [target-db]
#
# Config, same environment-variable contract as backup-postgres.sh so both
# scripts can be driven identically by the box (via
# /opt/a2z-core/backup.env) and by tests/integration/backup/test_restore_drill.py:
#   PGUSER       -- Postgres user (default: a2z)
#   COMPOSE_FILE -- docker-compose file the box's postgres service lives in
#   PG_EXEC      -- command prefix to run psql with. Defaults to
#                   `docker compose -f "$COMPOSE_FILE" exec -T postgres`.
#                   Tests set this to "" to run the local psql binary
#                   directly (against PGHOST/PGPORT/PGPASSWORD from the
#                   environment -- libpq reads those itself; this script
#                   never passes -h/-p so it works unchanged in-container).
#   S3_BUCKET    -- source bucket when the first argument is an S3 key
#                   rather than an existing local path. If the argument is
#                   a local file, S3 is never touched.
#   BACKUP_DIR   -- local scratch directory for the fetched/decompressed
#                   dump (default: /tmp).
set -euo pipefail

PGUSER="${PGUSER:-a2z}"
COMPOSE_FILE="${COMPOSE_FILE:-/opt/a2z-core/docker-compose.yml}"
BACKUP_DIR="${BACKUP_DIR:-/tmp}"

if [ -z "${PG_EXEC+set}" ]; then
  PG_EXEC_ARR=(docker compose -f "$COMPOSE_FILE" exec -T postgres)
elif [ -z "$PG_EXEC" ]; then
  PG_EXEC_ARR=()
else
  read -ra PG_EXEC_ARR <<< "$PG_EXEC"
fi

usage() {
  echo "Usage: $0 <s3-key|local-path-to-.sql[.gz]> [target-db]" >&2
  exit 1
}

SOURCE="${1:-}"
[ -n "$SOURCE" ] || usage
TARGET_DB="${2:-a2z_restore_$(date +%s)}"

# --- Step 1: obtain a local, decompressed .sql file ---
if [ -f "$SOURCE" ]; then
  LOCAL_FILE="$SOURCE"
else
  [ -n "${S3_BUCKET:-}" ] || {
    echo "'$SOURCE' is not a local file and S3_BUCKET is not set" >&2
    exit 1
  }
  LOCAL_FILE="$BACKUP_DIR/$(basename "$SOURCE")"
  aws s3 cp "s3://$S3_BUCKET/backups/postgres/$SOURCE" "$LOCAL_FILE"
fi

if [[ "$LOCAL_FILE" == *.gz ]]; then
  gunzip -k -f "$LOCAL_FILE"
  LOCAL_FILE="${LOCAL_FILE%.gz}"
fi

echo "Restoring $LOCAL_FILE into database '$TARGET_DB'..."

# --- Step 2: fresh target database. Restoring onto an existing database
#     (especially the live one) would collide with objects already there
#     -- this is the whole reason a restore drill exists: to force
#     "restore into a clean database, verify, then cut over" as the only
#     supported procedure. DROP+CREATE run as separate autocommitted
#     statements (no -1/--single-transaction here), which is required
#     since DROP/CREATE DATABASE cannot run inside a transaction block.
#     WITH (FORCE) (PG13+) so a lingering connection from a prior drill run
#     against the same target name can't leave this stuck. ---
${PG_EXEC_ARR[@]+"${PG_EXEC_ARR[@]}"} psql -U "$PGUSER" -d postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS \"$TARGET_DB\" WITH (FORCE);" \
  -c "CREATE DATABASE \"$TARGET_DB\";"

# --- Step 3: restore. ON_ERROR_STOP + --single-transaction together are
#     load-bearing: without them a half-applied restore can exit 0 with
#     silently missing data, which is exactly the failure mode this whole
#     effort exists to catch. ---
${PG_EXEC_ARR[@]+"${PG_EXEC_ARR[@]}"} psql -U "$PGUSER" -d "$TARGET_DB" \
  -v ON_ERROR_STOP=1 --single-transaction < "$LOCAL_FILE"

# --- Step 4: sanity summary for the operator to eyeball before swapping
#     DATABASE_URL. Both alembic_version rows are printed explicitly
#     because a dump carries the schema version it was taken at -- if
#     that's behind the app image's expected head, `alembic upgrade head`
#     for BOTH services is required before the app will run correctly
#     against this restored database (see DEPLOYMENT.md). ---
echo
echo "=== Restore summary: $TARGET_DB ==="
${PG_EXEC_ARR[@]+"${PG_EXEC_ARR[@]}"} psql -U "$PGUSER" -d "$TARGET_DB" -v ON_ERROR_STOP=1 <<'SQL'
SELECT table_schema, count(*) AS tables
FROM information_schema.tables
WHERE table_schema IN ('omnichannel', 'invoicing')
GROUP BY table_schema
ORDER BY table_schema;

SELECT 'omnichannel' AS service, version_num FROM omnichannel.alembic_version
UNION ALL
SELECT 'invoicing' AS service, version_num FROM invoicing.alembic_version;
SQL

echo
echo "Restored into '$TARGET_DB'. Verify the table/version summary above,"
echo "run any pending 'alembic upgrade head' for each service, then point"
echo "DATABASE_URL at '$TARGET_DB' and restart a2z-core. See DEPLOYMENT.md's"
echo "backup runbook for the full cutover procedure."
