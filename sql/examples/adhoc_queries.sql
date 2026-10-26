-- Ad hoc queries for the duty officer / reviewer. Run: psql -f sql/examples/adhoc_queries.sql
-- (`psql -v day=2022-07-30 -v pcode=UG3020 -f ...` to choose the date and district.)
\set ON_ERROR_STOP on
\if :{?day}
\else
    \set day '2022-07-30'
\endif
\if :{?pcode}
\else
    SELECT pcode AS pcode FROM ews.v_flagged ORDER BY rank DESC, ratio_to_p95 DESC LIMIT 1 \gset
\endif
\pset pager off
SET client_min_messages = warning;

\echo '--- Step 3, spelled out: ST_Clip + ST_SummaryStats per tile, then combined per district'
-- Each district usually spans several 100x100 tiles. Per-tile stats are combined with a
-- pixel-count-weighted mean; ST_SummaryStatsAgg (used in ews.compute_district_stats) does
-- the same in one aggregate.
WITH per_tile AS (
    SELECT d.name, s.*
      FROM ref.district d
      JOIN rain.chirps_daily r
        ON r.obs_date = :'day'::date AND ST_Intersects(r.rast, d.geom)
     CROSS JOIN LATERAL ST_SummaryStats(ST_Clip(r.rast, 1, d.geom, true), 1, true) s
     WHERE d.pcode = :'pcode'
)
SELECT name, sum(count) AS pixels,
       round((sum(mean * count) / nullif(sum(count), 0))::numeric, 1) AS mean_mm,
       round(max(max)::numeric, 1) AS max_mm, count(*) AS tiles
  FROM per_tile WHERE count > 0 GROUP BY name;

\echo '--- Same thing with the aggregate'
SELECT d.name, round((s).mean::numeric, 1) AS mean_mm, round((s).max::numeric, 1) AS max_mm, (s).count AS pixels
  FROM (SELECT d.district_id, ST_SummaryStatsAgg(ST_Clip(r.rast, 1, d.geom, true), 1, true) AS s
          FROM ref.district d
          JOIN rain.chirps_daily r ON r.obs_date = :'day'::date AND ST_Intersects(r.rast, d.geom)
         WHERE d.pcode = :'pcode' GROUP BY d.district_id) x
  JOIN ref.district d USING (district_id);

\echo '--- Step 4, simplest form: 3-day running total of district means vs a fixed 80 mm trigger'
SELECT obs_date, round(mean_mm::numeric, 1) AS daily_mm,
       round(sum(mean_mm) OVER w::numeric, 1) AS total_3d_mm,
       count(*) OVER w = 3 AND sum(mean_mm) OVER w >= 80 AS fixed_trigger
  FROM ews.district_raster_stats s JOIN ref.district d USING (district_id)
 WHERE d.pcode = :'pcode' AND s.product = 'chirps'
WINDOW w AS (ORDER BY obs_date RANGE BETWEEN INTERVAL '2 days' PRECEDING AND CURRENT ROW)
 ORDER BY obs_date;

\echo '--- Results table: flagged districts on the day'
SELECT district, alert_level, accum_mean_mm, p95_mean_mm, ratio_to_p95, pct_area_above_p95, triggered_rules
  FROM ews.v_flagged WHERE end_date = :'day'::date ORDER BY rank DESC, ratio_to_p95 DESC;

\echo '--- Index use: the raster convex-hull GiST index serves ST_Intersects(rast, geom)'
EXPLAIN (COSTS OFF)
SELECT ST_SummaryStatsAgg(ST_Clip(r.rast, 1, d.geom, true), 1, true)
  FROM ref.district d
  JOIN rain.chirps_daily r ON r.obs_date = :'day'::date AND ST_Intersects(r.rast, d.geom)
 WHERE d.pcode = :'pcode';

\echo '--- Operations log: faults, actions and escalations in the last 7 days'
SELECT logged_at::timestamp(0) AS logged_at, category, severity, component, message
  FROM ops.operations_log
 WHERE category IN ('FAULT', 'ACTION', 'ESCALATION') AND logged_at > now() - interval '7 days'
 ORDER BY logged_at DESC LIMIT 20;

\echo '--- CAP lifecycle per district'
SELECT district, msg_type, status, sent::timestamp(0) AS sent, severity, is_latest, identifier
  FROM ews.v_cap_lifecycle ORDER BY district, sent;
