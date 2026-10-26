#!/usr/bin/env bash
# Re-ingest one day of CHIRPS (corrupt, missing or superseded file) and recompute what depends on it.
#   scripts/reingest_date.sh 2022-07-30            re-download, then reload
#   scripts/reingest_date.sh 2022-07-30 --local    reload the file already in $DATA_DIR
#
# The delete and the append run in ONE transaction: if the new file violates the raster
# constraints added at load time (-C: SRID, scale, block size, alignment, extent), nothing
# changes and the previous tiles stay in place.
source "$(dirname "$0")/lib.sh"
require raster2pgsql psql

DAY="${1:?usage: $0 YYYY-MM-DD [--local]}"
date -d "$DAY" +%F >/dev/null 2>&1 || die "bad date: $DAY"
STAMP="$(date -d "$DAY" +%Y.%m.%d)"

oplog ACTION chirps-feed INFO "Re-ingest of $DAY started" "{\"date\": \"$DAY\"}"

if [[ "${2:-}" != "--local" ]]; then
    "$PY" -m ews fetch-chirps --start "$DAY" --end "$DAY" --force
fi

shopt -s nullglob
files=( "$DATA_DIR"/chirps/daily/*."$STAMP".tif )
(( ${#files[@]} == 1 )) || die "expected exactly one file for $STAMP in $DATA_DIR/chirps/daily, found ${#files[@]}"
log "replacing tiles for $DAY with ${files[0]##*/}"

{
    echo "DELETE FROM rain.chirps_daily WHERE obs_date = DATE '$DAY';"
    raster2pgsql -s 4326 -t 100x100 -P -F -a -e "${files[0]}" rain.chirps_daily
    echo "SELECT rain.register_daily_table('rain.chirps_daily');"
} | psql_q --single-transaction >/dev/null \
  || { oplog FAULT chirps-feed ERROR "Re-ingest of $DAY rejected; previous tiles kept" "{\"date\": \"$DAY\", \"file\": \"${files[0]}\"}"; exit 1; }

# Daily stats for DAY, and every 3-day window that contains DAY (ending DAY .. DAY+2).
"$PY" -m ews compute --start "$DAY" --end "$(date -d "$DAY + 2 days" +%F)"
"$PY" -m ews integrity || true
oplog ACTION chirps-feed INFO "Re-ingest of $DAY completed" "{\"date\": \"$DAY\", \"file\": \"${files[0]##*/}\"}"
