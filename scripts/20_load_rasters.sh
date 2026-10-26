#!/usr/bin/env bash
# Step 2: load rainfall rasters with raster2pgsql (-t 100x100 -I -C).
#
#   rain.chirps_daily  daily CHIRPS GeoTIFFs (Uganda window), one row per 100x100 tile per day
#   rain.clim_p95_3d   climatology: band 1 = P95 of 3-day totals, band 2 = mean of 3-day totals
#
# Flags: -s SRID, -t tile size, -P pad edge tiles to full size (keeps blocks regular),
#        -I GiST index on ST_ConvexHull(rast), -C raster constraints, -F filename column,
#        -M VACUUM ANALYZE, -d drop and recreate (full, idempotent reload).
source "$(dirname "$0")/lib.sh"
require raster2pgsql psql

DAILY_DIR="$DATA_DIR/chirps/daily"
CLIM_DIR="$DATA_DIR/clim"

shopt -s nullglob
daily=( "$DAILY_DIR"/*.tif )
clim=( "$CLIM_DIR"/clim_*.tif )
(( ${#daily[@]} )) || die "no GeoTIFFs in $DAILY_DIR (run 'python -m ews fetch-chirps' or 'make demo-data')"

log "raster2pgsql: ${#daily[@]} daily files -> rain.chirps_daily"
raster2pgsql -s 4326 -t 100x100 -P -I -C -M -F -d "${daily[@]}" rain.chirps_daily | psql_q >/dev/null
psql_q -At -c "SELECT rain.register_daily_table('rain.chirps_daily')" | while read -r n; do log "registered $n tiles"; done

if (( ${#clim[@]} )); then
    log "raster2pgsql: ${#clim[@]} climatology file(s) -> rain.clim_p95_3d"
    raster2pgsql -s 4326 -t 100x100 -P -I -C -M -F -d "${clim[@]}" rain.clim_p95_3d | psql_q >/dev/null
    psql_q -At -c "SELECT rain.register_climatology()" | while read -r n; do log "registered $n climatology raster(s)"; done
else
    log "WARNING: no climatology in $CLIM_DIR - run 'python -m ews climatology' before compute"
fi

summary=$(psql_q -At -F ' ' -c "SELECT count(*), count(DISTINCT obs_date), min(obs_date), max(obs_date), min(product_source) FROM rain.chirps_daily")
read -r tiles days dmin dmax src <<< "$summary"
oplog INGEST chirps-feed INFO "Loaded $tiles tiles / $days days ($dmin..$dmax) into rain.chirps_daily" \
      "{\"tiles\": $tiles, \"days\": $days, \"first\": \"$dmin\", \"last\": \"$dmax\", \"source\": \"$src\", \"climatology_files\": ${#clim[@]}}"

psql_q <<'SQL'
\echo
\echo raster_columns (constraints added by -C):
SELECT r_table_name AS table, srid, scale_x, blocksize_x || 'x' || blocksize_y AS block,
       num_bands, same_alignment, regular_blocking, nodata_values
  FROM raster_columns WHERE r_table_schema = 'rain' ORDER BY 1;
SQL
