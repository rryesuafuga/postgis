#!/usr/bin/env bash
# Shared helpers for the shell steps. Source it: `source "$(dirname "$0")/lib.sh"`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Load .env without overriding variables already exported by the caller (e.g. make demo).
if [[ -f "$ROOT/.env" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# || "$line" != *=* ]] && continue
        key="${line%%=*}"; val="${line#*=}"
        [[ -z "${!key:-}" ]] && export "$key=$val"
    done < "$ROOT/.env"
fi

export PGHOST="${PGHOST:-localhost}" PGPORT="${PGPORT:-5433}" PGDATABASE="${PGDATABASE:-ews}"
export PGUSER="${PGUSER:-ews}" PGPASSWORD="${PGPASSWORD:-ews}"

DATA_DIR="${EWS_DATA_DIR:-data}"
[[ "$DATA_DIR" = /* ]] || DATA_DIR="$ROOT/$DATA_DIR"
# shellcheck disable=SC2034  # used by the scripts that source this file
PY="${PYTHON:-python3}"

log()  { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
die()  { log "ERROR: $*"; exit 1; }

# NOTICEs from raster2pgsql/AddRasterConstraints are expected noise; warnings and errors still show.
export PGOPTIONS="${PGOPTIONS:--c client_min_messages=warning}"

psql_q() { psql -X -v ON_ERROR_STOP=1 -q "$@"; }

# oplog CATEGORY COMPONENT SEVERITY MESSAGE [DETAILS_JSON]  -> ops.operations_log
oplog() {
    psql_q -v cat="$1" -v comp="$2" -v sev="$3" -v msg="$4" -v det="${5:-null}" >/dev/null <<'SQL'
SELECT ops.log(:'cat', :'comp', :'sev', :'msg', nullif(:'det', 'null')::jsonb);
SQL
}

require() {
    for bin in "$@"; do
        command -v "$bin" >/dev/null 2>&1 || die "'$bin' not found. Install GDAL/PostGIS client tools, or run inside the tools container: docker compose --profile tools run --rm tools <command>"
    done
}
