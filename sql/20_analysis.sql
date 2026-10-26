-- District rainfall statistics, N-day accumulations and exceedance flags.
--
--   ews.compute_district_stats(table, product, start, end, run_id)    step 3: ST_Clip + ST_SummaryStatsAgg
--   ews.build_accumulations(start, end, days)                         N-day totals per tile, in-database
--   ews.compute_exceedance(start, end, run_id, days, climatology)     step 4: vs P95 climatology + fixed triggers
--
-- Districts smaller than a pixel (no pixel centre falls inside the polygon, so ST_Clip keeps
-- nothing) fall back to the pixel under ST_PointOnSurface(geom); pixel_count = 0, method = 'point'.

-- Derived N-day accumulation rasters (same grid and tiling as rain.chirps_daily).
CREATE TABLE IF NOT EXISTS rain.chirps_accum (
    rid        serial PRIMARY KEY,
    end_date   date     NOT NULL,
    accum_days smallint NOT NULL,
    rast       raster   NOT NULL
);
CREATE INDEX IF NOT EXISTS chirps_accum_date_idx ON rain.chirps_accum (accum_days, end_date);
CREATE INDEX IF NOT EXISTS chirps_accum_hull_gist ON rain.chirps_accum USING gist (ST_ConvexHull(rast));

------------------------------------------------------------------------------
-- Step 3: zonal statistics for any daily raster table with an obs_date column
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ews.compute_district_stats(
    p_table   regclass,
    p_product text,
    p_start   date,
    p_end     date,
    p_run_id  bigint DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql
SET client_min_messages = warning   -- ST_Clip notices on all-nodata clips are expected
AS $$
DECLARE
    v_n integer;
BEGIN
    EXECUTE format($q$
        WITH clipped AS (
            -- A district can span several 100x100 tiles: clip each tile, then aggregate the
            -- partial statistics across tiles with ST_SummaryStatsAgg.
            SELECT d.district_id, r.obs_date,
                   ST_SummaryStatsAgg(ST_Clip(r.rast, 1, d.geom, true), 1, true) AS s
              FROM ref.district d
              JOIN %1$s r
                ON r.obs_date BETWEEN $1 AND $2
               AND ST_Intersects(r.rast, d.geom)          -- GiST on ST_ConvexHull(rast)
             GROUP BY d.district_id, r.obs_date
        ), filled AS (
            SELECT c.district_id, c.obs_date, c.s, p.val
              FROM clipped c
              LEFT JOIN LATERAL (
                    SELECT ST_Value(r.rast, 1, pt.geom) AS val
                      FROM ref.district d
                     CROSS JOIN LATERAL (SELECT ST_PointOnSurface(d.geom) AS geom) pt
                      JOIN %1$s r ON r.obs_date = c.obs_date AND ST_Intersects(r.rast, pt.geom)
                     WHERE d.district_id = c.district_id
                       AND coalesce((c.s).count, 0) = 0
                     LIMIT 1) p ON true
        )
        INSERT INTO ews.district_raster_stats AS t
            (district_id, product, obs_date, mean_mm, max_mm, min_mm, stddev_mm,
             pixel_count, method, run_id, computed_at)
        SELECT district_id, $3, obs_date,
               CASE WHEN (s).count > 0 THEN (s).mean   ELSE val END,
               CASE WHEN (s).count > 0 THEN (s).max    ELSE val END,
               CASE WHEN (s).count > 0 THEN (s).min    ELSE val END,
               CASE WHEN (s).count > 0 THEN (s).stddev ELSE 0 END,
               coalesce((s).count, 0),
               CASE WHEN (s).count > 0 THEN 'clip' ELSE 'point' END,
               $4, now()
          FROM filled
         WHERE (s).count > 0 OR val IS NOT NULL
        ON CONFLICT (district_id, product, obs_date) DO UPDATE SET
            mean_mm = EXCLUDED.mean_mm, max_mm = EXCLUDED.max_mm, min_mm = EXCLUDED.min_mm,
            stddev_mm = EXCLUDED.stddev_mm, pixel_count = EXCLUDED.pixel_count,
            method = EXCLUDED.method, run_id = EXCLUDED.run_id, computed_at = EXCLUDED.computed_at
        $q$, p_table)
    USING p_start, p_end, p_product, p_run_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END $$;

------------------------------------------------------------------------------
-- N-day accumulations, tile by tile
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ews.build_accumulations(
    p_start date,
    p_end   date,
    p_days  integer DEFAULT 3)
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_n integer;
BEGIN
    DELETE FROM rain.chirps_accum
     WHERE accum_days = p_days AND end_date BETWEEN p_start AND p_end;

    -- ST_Union(...,'SUM') skips nodata pixels, which would silently under-count a pixel
    -- missing on one day; the COUNT union masks any pixel without all N valid days.
    INSERT INTO rain.chirps_accum (end_date, accum_days, rast)
    SELECT end_date, p_days,
           ST_MapAlgebra(sum_r, 1, cnt_r, 1,
                         format('CASE WHEN [rast2.val] = %s THEN [rast1.val] ELSE NULL END', p_days),
                         '32BF', 'FIRST', NULL, NULL, NULL)
      FROM (
            SELECT e.end_date::date AS end_date,
                   ST_Union(c.rast, 'SUM')   AS sum_r,
                   ST_Union(c.rast, 'COUNT') AS cnt_r
              FROM generate_series(p_start, p_end, interval '1 day') AS e(end_date)
              JOIN rain.chirps_daily c
                ON c.obs_date BETWEEN e.end_date::date - (p_days - 1) AND e.end_date::date
             GROUP BY e.end_date, ST_UpperLeftX(c.rast), ST_UpperLeftY(c.rast)
            -- exactly one tile per day: a duplicated re-ingest must not double-count
            HAVING count(*) = p_days AND count(DISTINCT c.obs_date) = p_days
           ) u;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END $$;

------------------------------------------------------------------------------
-- Pick the climatology whose seasonal window covers the analysis period
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ews.pick_climatology(p_start date, p_end date, p_days integer DEFAULT 3)
RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT filename
      FROM rain.climatology_catalog c,
           LATERAL (SELECT to_char(p_start, 'MMDD') AS s, to_char(p_end, 'MMDD') AS e) d
     WHERE c.accum_days = p_days
       AND CASE WHEN c.window_start <= c.window_end            -- window inside one calendar year
                THEN d.s >= c.window_start AND d.e <= c.window_end AND d.s <= d.e
                ELSE (d.s >= c.window_start OR d.s <= c.window_end)   -- window wraps 31 Dec
                 AND (d.e >= c.window_start OR d.e <= c.window_end) END
     ORDER BY c.registered_at DESC, c.filename
     LIMIT 1;
$$;

------------------------------------------------------------------------------
-- Step 4/5: exceedance vs climatology and fixed triggers -> ews.district_exceedance
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ews.compute_exceedance(
    p_start       date,
    p_end         date,
    p_run_id      bigint  DEFAULT NULL,
    p_days        integer DEFAULT 3,
    p_climatology text    DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql
SET client_min_messages = warning
AS $$
DECLARE
    v_clim text := coalesce(p_climatology, ews.pick_climatology(p_start, p_end, p_days));
    v_n    integer;
BEGIN
    IF v_clim IS NULL THEN
        RAISE EXCEPTION 'No %-day climatology covers % .. %: build one with `python -m ews climatology`',
                        p_days, p_start, p_end;
    END IF;

    PERFORM ews.build_accumulations(p_start, p_end, p_days);

    WITH tiles AS (
        -- Pixel-wise exceedance (1/0) of the accumulation over the pixel P95 (band 1).
        SELECT a.end_date, a.rast AS acc, c.rast AS clim,
               ST_MapAlgebra(a.rast, 1, c.rast, 1,
                             'CASE WHEN [rast1.val] > [rast2.val] THEN 1 ELSE 0 END',
                             '32BF', 'FIRST', NULL, NULL, NULL) AS exc
          FROM rain.chirps_accum a
          JOIN rain.clim_p95_3d c
            ON c.filename = v_clim
           AND ST_UpperLeftX(c.rast) = ST_UpperLeftX(a.rast)
           AND ST_UpperLeftY(c.rast) = ST_UpperLeftY(a.rast)
         WHERE a.accum_days = p_days AND a.end_date BETWEEN p_start AND p_end
    ), zonal AS (
        -- The 2-band climatology is clipped once (all bands) and summarised per band.
        -- (Also avoids ST_Clip(rast, 2, geom), which crashed the backend with the Ubuntu 24.04
        -- PostGIS 3.4.2 package; the postgis/postgis:16-3.4 image (3.4.3) was not affected.)
        SELECT d.district_id, t.end_date,
               ST_SummaryStatsAgg(ST_Clip(t.acc, 1, d.geom, true), 1, true) AS acc_s,
               ST_SummaryStatsAgg(cc.clim, 1, true)                         AS p95_s,
               ST_SummaryStatsAgg(cc.clim, 2, true)                         AS mean_s,
               ST_SummaryStatsAgg(ST_Clip(t.exc, 1, d.geom, true), 1, true) AS exc_s
          FROM ref.district d
          JOIN tiles t ON ST_Intersects(t.acc, d.geom)
         CROSS JOIN LATERAL (SELECT ST_Clip(t.clim, d.geom, true) AS clim) cc
         GROUP BY d.district_id, t.end_date
    ), metrics AS (
        SELECT z.district_id, z.end_date,
               coalesce((z.acc_s).count, 0) AS pixel_count,
               CASE WHEN (z.acc_s).count > 0 THEN 'clip' ELSE 'point' END AS method,
               CASE WHEN (z.acc_s).count > 0 THEN (z.acc_s).mean  ELSE p.acc  END AS accum_mean,
               CASE WHEN (z.acc_s).count > 0 THEN (z.acc_s).max   ELSE p.acc  END AS accum_max,
               CASE WHEN (z.acc_s).count > 0 THEN (z.p95_s).mean  ELSE p.p95  END AS p95_mean,
               CASE WHEN (z.acc_s).count > 0 THEN (z.mean_s).mean ELSE p.cmean END AS clim_mean,
               CASE WHEN (z.acc_s).count > 0 THEN (z.exc_s).mean
                    ELSE (p.acc > p.p95)::int END AS frac_above
          FROM zonal z
          LEFT JOIN LATERAL (
                SELECT ST_Value(t.acc, 1, pt.geom)  AS acc,
                       ST_Value(t.clim, 1, pt.geom) AS p95,
                       ST_Value(t.clim, 2, pt.geom) AS cmean
                  FROM ref.district d
                 CROSS JOIN LATERAL (SELECT ST_PointOnSurface(d.geom) AS geom) pt
                  JOIN tiles t ON t.end_date = z.end_date AND ST_Intersects(t.acc, pt.geom)
                 WHERE d.district_id = z.district_id AND coalesce((z.acc_s).count, 0) = 0
                 LIMIT 1) p ON true
    ), hits AS (
        SELECT m.district_id, m.end_date, r.rule_code, l.level, l.rank
          FROM metrics m
          JOIN ews.trigger_rule r ON r.active AND r.accum_days = p_days
          JOIN ews.alert_level  l ON l.level = r.level
         WHERE CASE r.metric
                    WHEN 'mean_over_p95'      THEN m.accum_mean >  r.threshold * m.p95_mean
                    WHEN 'area_frac_over_p95' THEN m.frac_above >= r.threshold
                    WHEN 'max_mm'             THEN m.accum_max  >= r.threshold
                    WHEN 'mean_mm'            THEN m.accum_mean >= r.threshold
               END
           AND m.accum_mean >= r.min_accum_mm
    ), flags AS (
        SELECT district_id, end_date,
               array_agg(rule_code ORDER BY rank DESC, rule_code) AS rules,
               (array_agg(level ORDER BY rank DESC))[1]           AS level
          FROM hits
         GROUP BY district_id, end_date
    )
    INSERT INTO ews.district_exceedance AS x
        (district_id, end_date, accum_days, accum_mean_mm, accum_max_mm, p95_mean_mm, clim_mean_mm,
         ratio_to_p95, frac_area_above_p95, pixel_count, method, climatology,
         triggered_rules, alert_level, run_id, computed_at)
    SELECT m.district_id, m.end_date, p_days, m.accum_mean, m.accum_max, m.p95_mean, m.clim_mean,
           m.accum_mean / nullif(m.p95_mean, 0), m.frac_above, m.pixel_count, m.method, v_clim,
           coalesce(f.rules, '{}'), coalesce(f.level, 'none'), p_run_id, now()
      FROM metrics m
      LEFT JOIN flags f USING (district_id, end_date)
     WHERE m.accum_mean IS NOT NULL
    ON CONFLICT (district_id, end_date, accum_days) DO UPDATE SET
        accum_mean_mm = EXCLUDED.accum_mean_mm, accum_max_mm = EXCLUDED.accum_max_mm,
        p95_mean_mm = EXCLUDED.p95_mean_mm, clim_mean_mm = EXCLUDED.clim_mean_mm,
        ratio_to_p95 = EXCLUDED.ratio_to_p95, frac_area_above_p95 = EXCLUDED.frac_area_above_p95,
        pixel_count = EXCLUDED.pixel_count, method = EXCLUDED.method,
        climatology = EXCLUDED.climatology, triggered_rules = EXCLUDED.triggered_rules,
        alert_level = EXCLUDED.alert_level, run_id = EXCLUDED.run_id, computed_at = EXCLUDED.computed_at;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END $$;

------------------------------------------------------------------------------
-- Convenience views
------------------------------------------------------------------------------
CREATE OR REPLACE VIEW ews.v_flagged AS
SELECT x.end_date, d.pcode, d.name AS district, d.region, x.alert_level, l.rank,
       round(x.accum_mean_mm::numeric, 1)          AS accum_mean_mm,
       round(x.accum_max_mm::numeric, 1)           AS accum_max_mm,
       round(x.p95_mean_mm::numeric, 1)            AS p95_mean_mm,
       round(x.ratio_to_p95::numeric, 2)           AS ratio_to_p95,
       round(100 * x.frac_area_above_p95::numeric) AS pct_area_above_p95,
       x.triggered_rules, x.district_id
  FROM ews.district_exceedance x
  JOIN ref.district d USING (district_id)
  JOIN ews.alert_level l ON l.level = x.alert_level
 WHERE x.alert_level <> 'none';

-- Issued messages per district (Draft templates excluded); is_latest marks the current state.
CREATE OR REPLACE VIEW ews.v_cap_lifecycle AS
SELECT m.district_id, d.name AS district, m.identifier, m.msg_type, m.status, m.sent,
       m.expires, m.severity, m.references_ids, m.approved_by, m.validated,
       row_number() OVER (PARTITION BY m.district_id ORDER BY m.sent DESC, m.created_at DESC) = 1 AS is_latest
  FROM ews.cap_message m
  LEFT JOIN ref.district d USING (district_id)
 WHERE m.status <> 'Draft';
