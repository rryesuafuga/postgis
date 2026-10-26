#!/usr/bin/env bash
# Step 1: load district boundaries with ogr2ogr, normalise them, and build the GiST index.
#
# The boundary source is described by $DATA_DIR/boundaries/district_source.env, written by
# `python -m ews fetch-districts` (HDX COD-AB or GADM) or `python -m ews synthetic`.
source "$(dirname "$0")/lib.sh"
require ogr2ogr psql

SRC_ENV="$DATA_DIR/boundaries/district_source.env"
[[ -f "$SRC_ENV" ]] || die "$SRC_ENV not found: run 'python -m ews fetch-districts' (or 'make demo-data')"
# shellcheck disable=SC1090
source "$SRC_ENV"

log "ogr2ogr: $EWS_DISTRICT_SRC ${EWS_DISTRICT_LAYER:-} -> ref.district_src"
# Staging load. The spatial index is created explicitly on the normalised table below.
ogr2ogr -f PostgreSQL \
    PG:"host=$PGHOST port=$PGPORT dbname=$PGDATABASE user=$PGUSER" \
    "$EWS_DISTRICT_SRC" ${EWS_DISTRICT_LAYER:+"$EWS_DISTRICT_LAYER"} \
    -nln ref.district_src -overwrite \
    -nlt PROMOTE_TO_MULTI -t_srs EPSG:4326 -makevalid \
    -lco GEOMETRY_NAME=geom -lco FID=gid -lco SPATIAL_INDEX=NONE -lco LAUNDER=YES \
    --config PG_USE_COPY YES

psql_q -v label="$EWS_DISTRICT_SOURCE_LABEL" -v scheme="$EWS_DISTRICT_CODE_SCHEME" \
       -v name="${EWS_DISTRICT_NAME_FIELD:-}" -v code="${EWS_DISTRICT_CODE_FIELD:-}" \
       -v region="${EWS_DISTRICT_REGION_FIELD:-}" <<'SQL'
SELECT ref.normalize_districts(:'label', :'scheme', :'name', :'code', :'region') AS districts_loaded;

-- Step 1 requirement: GiST index on the district geometry (no-op if it already exists).
CREATE INDEX IF NOT EXISTS district_geom_gist ON ref.district USING gist (geom);
-- Physically order rows by the index: neighbouring districts end up in neighbouring pages.
CLUSTER ref.district USING district_geom_gist;
ANALYZE ref.district;

\echo
SELECT count(*) AS districts, round(sum(area_km2)) AS total_km2,
       count(*) FILTER (WHERE NOT ST_IsValid(geom)) AS invalid,
       min(source) AS source
  FROM ref.district;
SQL
