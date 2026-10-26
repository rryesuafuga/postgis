#!/usr/bin/env bash
# Backup with verification:
#   1. pg_dump (custom format) + SHA-256 checksum
#   2. pg_restore --list (archive TOC readable)
#   3. restore into a scratch database, compare row counts, run ops.integrity_checks() there
#   4. log the outcome to ops.operations_log (category BACKUP, details.verified = true/false)
#   5. keep the newest $EWS_BACKUP_KEEP dumps (default 7)
source "$(dirname "$0")/lib.sh"
require pg_dump pg_restore psql createdb dropdb sha256sum

BACKUP_DIR="${EWS_BACKUP_DIR:-$ROOT/backups}"
KEEP="${EWS_BACKUP_KEEP:-7}"
VERIFY_DB="${PGDATABASE}_verify"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP="$BACKUP_DIR/${PGDATABASE}_${TS}.dump"
mkdir -p "$BACKUP_DIR"

[[ "$(psql_q -At -c "SELECT to_regprocedure('ops.table_counts()') IS NOT NULL")" == t ]] \
    || die "database '$PGDATABASE' is not initialised (run scripts/00_init_db.sh)"

fail() {
    log "BACKUP FAILED: $*"
    # An unverified dump must never be mistaken for a good one.
    [[ -f "$DUMP" ]] && mv "$DUMP" "$DUMP.failed" && rm -f "$DUMP.sha256" "$DUMP.toc"
    oplog BACKUP backup ERROR "Backup failed: $*" "{\"file\": \"$DUMP\", \"verified\": false}" || true
    exit 1
}
STAGE=pg_dump
trap 'fail "$STAGE step failed (line $LINENO)"' ERR

log "pg_dump -> $DUMP"
pg_dump -Fc -Z 6 --no-owner -f "$DUMP"
sha256sum "$DUMP" | awk '{print $1}' > "$DUMP.sha256"
BYTES=$(stat -c %s "$DUMP")

STAGE="archive TOC check"
log "pg_restore --list (TOC check)"
pg_restore --list "$DUMP" > "$DUMP.toc"
TOC_ENTRIES=$(grep -cv '^;' "$DUMP.toc" || true)

STAGE="verification restore"
log "verification restore into $VERIFY_DB"
dropdb --if-exists "$VERIFY_DB"
createdb "$VERIFY_DB"
pg_restore --exit-on-error --no-owner -d "$VERIFY_DB" "$DUMP"

STAGE="row count comparison"
PROD_COUNTS=$(psql_q -At -F= -c "SELECT * FROM ops.table_counts()")
VER_COUNTS=$(psql_q -d "$VERIFY_DB" -At -F= -c "SELECT * FROM ops.table_counts()")
[[ "$PROD_COUNTS" == "$VER_COUNTS" ]] || fail "row counts differ: prod [$PROD_COUNTS] vs restored [$VER_COUNTS]"
log "row counts match: $(echo "$VER_COUNTS" | tr '\n' ' ')"

FAILS=$(psql_q -d "$VERIFY_DB" -At -c "SELECT string_agg(check_name, ', ') FROM ops.integrity_checks() WHERE status = 'FAIL'")
[[ -z "$FAILS" ]] || fail "integrity checks FAIL on restored copy: $FAILS"
log "integrity checks on restored copy: no FAIL"

[[ "${EWS_KEEP_VERIFY_DB:-0}" == 1 ]] || dropdb "$VERIFY_DB"

# Retention: newest $KEEP dumps (and their sidecars) are kept.
ls -1t "$BACKUP_DIR"/"${PGDATABASE}"_*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
    log "retention: removing $old"; rm -f "$old" "$old.sha256" "$old.toc"
done

trap - ERR
COUNTS_JSON=$(echo "$VER_COUNTS" | awk -F= 'BEGIN{printf "{"} {printf "%s\"%s\": %s", (NR>1?", ":""), $1, $2} END{printf "}"}')
oplog BACKUP backup INFO "Backup verified: $(basename "$DUMP") ($BYTES bytes)" \
    "{\"file\": \"$DUMP\", \"bytes\": $BYTES, \"sha256\": \"$(cat "$DUMP.sha256")\", \"toc_entries\": $TOC_ENTRIES, \"row_counts\": $COUNTS_JSON, \"verified\": true}"
log "OK: $DUMP (sha256 $(cut -c1-16 < "$DUMP.sha256")...)"
