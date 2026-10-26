#!/usr/bin/env bash
# Create extensions, schemas, tables, functions and seed configuration (idempotent).
source "$(dirname "$0")/lib.sh"
require psql pg_isready

for i in $(seq 1 30); do
    pg_isready -q && break
    log "waiting for PostgreSQL at $PGHOST:$PGPORT ($i)"; sleep 2
done
pg_isready -q || die "PostgreSQL not reachable at $PGHOST:$PGPORT"

if [[ -z "$(psql -X -At -d postgres -c "SELECT 1 FROM pg_database WHERE datname = '$PGDATABASE'")" ]]; then
    log "creating database $PGDATABASE"
    createdb "$PGDATABASE"
fi

for f in 01_schema.sql 02_seed_config.sql 10_districts.sql 11_rasters.sql 20_analysis.sql 90_integrity.sql; do
    log "applying sql/$f"
    psql_q -f "$ROOT/sql/$f"
done

psql_q -At -c "SELECT 'PostGIS ' || postgis_lib_version() || ', raster ' || postgis_raster_lib_version()" | while read -r l; do log "$l"; done
oplog STATUS db INFO "Schema initialised/upgraded" "{\"script\": \"00_init_db.sh\"}"
