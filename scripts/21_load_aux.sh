#!/usr/bin/env bash
# Optional: load an auxiliary layer (GPM IMERG daily, ICPAC seasonal forecast, ...) prepared by
# `python -m ews prep-aux --name <name> ...` into rain.aux_<name>, then compute district stats.
#   scripts/21_load_aux.sh imerg
source "$(dirname "$0")/lib.sh"
require raster2pgsql psql

NAME="${1:?usage: $0 <layer-name>}"
[[ "$NAME" =~ ^[a-z0-9_]+$ ]] || die "layer name must match [a-z0-9_]+"
shopt -s nullglob
files=( "$DATA_DIR/aux/$NAME"/*.tif )
(( ${#files[@]} )) || die "no GeoTIFFs in $DATA_DIR/aux/$NAME"

log "raster2pgsql: ${#files[@]} file(s) -> rain.aux_$NAME"
raster2pgsql -s 4326 -t 100x100 -P -I -C -M -F -d "${files[@]}" "rain.aux_$NAME" | psql_q >/dev/null
psql_q -At -c "SELECT rain.register_daily_table('rain.aux_$NAME')" >/dev/null
oplog INGEST "aux:$NAME" INFO "Loaded ${#files[@]} file(s) into rain.aux_$NAME"
"$PY" -m ews compute-aux --name "$NAME"
