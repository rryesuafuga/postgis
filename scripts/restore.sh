#!/usr/bin/env bash
# Restore a verified dump over the live database (disaster recovery).
#   scripts/restore.sh backups/ews_20260101T000000Z.dump --yes
source "$(dirname "$0")/lib.sh"
require pg_restore psql sha256sum

DUMP="${1:?usage: $0 <dump-file> --yes}"
[[ "${2:-}" == "--yes" ]] || die "this replaces database '$PGDATABASE' on $PGHOST:$PGPORT; re-run with --yes"
[[ -f "$DUMP" ]] || die "$DUMP not found"

if [[ -f "$DUMP.sha256" ]]; then
    [[ "$(sha256sum "$DUMP" | awk '{print $1}')" == "$(cat "$DUMP.sha256")" ]] || die "checksum mismatch for $DUMP"
    log "checksum OK"
else
    log "WARNING: no checksum file for $DUMP"
fi

log "pg_restore --clean --if-exists -> $PGDATABASE"
pg_restore --clean --if-exists --no-owner --single-transaction --exit-on-error -d "$PGDATABASE" "$DUMP"
oplog ACTION db WARN "Database restored from $(basename "$DUMP")" "{\"file\": \"$DUMP\"}"
psql_q -c "SELECT * FROM ops.integrity_checks() WHERE status <> 'PASS'"
