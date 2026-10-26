-- Row counts of the data tables (backup verification compares these between live and restored).
-- The ops.* tables are excluded because they keep growing while the backup runs.
CREATE OR REPLACE FUNCTION ops.table_counts()
RETURNS TABLE (table_name text, row_count bigint)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    FOR table_name IN SELECT unnest(ARRAY[
            'ref.district', 'rain.chirps_daily', 'rain.clim_p95_3d', 'rain.chirps_accum',
            'ews.district_raster_stats', 'ews.district_exceedance', 'ews.cap_message'])
    LOOP
        IF to_regclass(table_name) IS NULL THEN
            row_count := -1;
        ELSE
            EXECUTE format('SELECT count(*) FROM %s', table_name) INTO row_count;
        END IF;
        RETURN NEXT;
    END LOOP;
END $$;

-- Geodatabase integrity checks. `SELECT * FROM ops.integrity_checks();`
-- Each row: check name, PASS / WARN / FAIL, detail. Wrapped by `python -m ews integrity`,
-- which logs the outcome to ops.operations_log and exits non-zero on any FAIL.

CREATE OR REPLACE FUNCTION ops.integrity_checks(p_expected_districts integer DEFAULT NULL)
RETURNS TABLE (check_name text, status text, detail text)
LANGUAGE plpgsql
SET client_min_messages = warning
AS $$
-- OUT columns (check_name, status, detail) must not shadow table columns of the same name.
#variable_conflict use_column
DECLARE
    v_int    bigint;
    v_int2   bigint;
    v_txt    text;
    v_bool   boolean;
    v_min    date;
    v_max    date;
    v_has_daily boolean := to_regclass('rain.chirps_daily') IS NOT NULL;
    v_has_clim  boolean := to_regclass('rain.clim_p95_3d') IS NOT NULL;
BEGIN
    -- 1. Districts ---------------------------------------------------------------------------
    SELECT count(*), string_agg(DISTINCT source, ', ') INTO v_int, v_txt FROM ref.district;
    check_name := 'districts.count';
    status := CASE WHEN v_int = 0 THEN 'FAIL'
                   WHEN p_expected_districts IS NOT NULL AND v_int <> p_expected_districts THEN 'WARN'
                   ELSE 'PASS' END;
    detail := format('%s districts (source: %s)%s', v_int, coalesce(v_txt, 'none'),
                     CASE WHEN p_expected_districts IS NOT NULL THEN format(', expected %s', p_expected_districts) ELSE '' END);
    RETURN NEXT;

    SELECT count(*) INTO v_int FROM ref.district WHERE NOT ST_IsValid(geom) OR ST_IsEmpty(geom);
    check_name := 'districts.geometry_valid';
    status := CASE WHEN v_int = 0 THEN 'PASS' ELSE 'FAIL' END;
    detail := format('%s invalid or empty geometries', v_int);
    RETURN NEXT;

    SELECT count(*) INTO v_int FROM ref.district WHERE ST_SRID(geom) <> 4326;
    check_name := 'districts.srid';
    status := CASE WHEN v_int = 0 THEN 'PASS' ELSE 'FAIL' END;
    detail := format('%s geometries not in EPSG:4326', v_int);
    RETURN NEXT;

    SELECT count(*) INTO v_int
      FROM ref.district a JOIN ref.district b
        ON a.district_id < b.district_id AND ST_Intersects(a.geom, b.geom)
       AND ST_Area(ST_Intersection(a.geom, b.geom)::geography) > 1e6;   -- > 1 km2 overlap
    check_name := 'districts.overlaps';
    status := CASE WHEN v_int = 0 THEN 'PASS' ELSE 'WARN' END;
    detail := format('%s district pairs overlap by more than 1 km2', v_int);
    RETURN NEXT;

    -- 2. Spatial indexes ----------------------------------------------------------------------
    FOR v_txt IN SELECT unnest(ARRAY['ref.district', 'rain.chirps_daily', 'rain.clim_p95_3d']) LOOP
        CONTINUE WHEN to_regclass(v_txt) IS NULL;
        SELECT count(*) INTO v_int
          FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_am am ON am.oid = c.relam
         WHERE i.indrelid = v_txt::regclass AND am.amname = 'gist';
        check_name := 'index.gist:' || v_txt;
        status := CASE WHEN v_int > 0 THEN 'PASS' ELSE 'FAIL' END;
        detail := format('%s GiST index(es)', v_int);
        RETURN NEXT;
    END LOOP;

    -- 3. Daily rasters ------------------------------------------------------------------------
    IF NOT v_has_daily THEN
        check_name := 'rain.chirps_daily'; status := 'FAIL'; detail := 'table missing - load rasters';
        RETURN NEXT;
    ELSE
        SELECT (srid = 4326 AND scale_x IS NOT NULL AND blocksize_x IS NOT NULL
                AND same_alignment AND regular_blocking IS NOT NULL AND num_bands = 1),
               format('srid=%s scale=%s/%s block=%sx%s same_alignment=%s nodata=%s',
                      srid, scale_x, scale_y, blocksize_x, blocksize_y, same_alignment, nodata_values)
          INTO v_bool, v_txt
          FROM raster_columns WHERE r_table_schema = 'rain' AND r_table_name = 'chirps_daily';
        check_name := 'rain.chirps_daily.constraints';
        status := CASE WHEN coalesce(v_bool, false) THEN 'PASS' ELSE 'FAIL' END;
        detail := coalesce(v_txt, 'no raster_columns entry (load without -C?)');
        RETURN NEXT;

        SELECT count(*) FILTER (WHERE obs_date IS NULL), min(obs_date), max(obs_date), count(DISTINCT obs_date)
          INTO v_int, v_min, v_max, v_int2 FROM rain.chirps_daily;
        check_name := 'rain.chirps_daily.dates';
        status := CASE WHEN v_int > 0 OR v_int2 = 0 THEN 'FAIL'
                       WHEN v_int2 < (v_max - v_min + 1) THEN 'WARN' ELSE 'PASS' END;
        detail := format('%s..%s, %s of %s days present, %s tiles without date%s',
                         v_min, v_max, v_int2, coalesce(v_max - v_min + 1, 0), v_int,
                         CASE WHEN v_int2 < (v_max - v_min + 1) THEN ': missing ' ||
                              (SELECT string_agg(g::date::text, ', ')
                                 FROM generate_series(v_min, v_max, interval '1 day') g
                                WHERE NOT EXISTS (SELECT 1 FROM rain.chirps_daily WHERE obs_date = g::date))
                              ELSE '' END);
        RETURN NEXT;

        SELECT count(*) INTO v_int FROM (
            SELECT obs_date FROM rain.chirps_daily GROUP BY obs_date
            HAVING count(*) <> (SELECT mode() WITHIN GROUP (ORDER BY n)
                                  FROM (SELECT count(*) n FROM rain.chirps_daily GROUP BY obs_date) t)
               OR count(*) <> count(DISTINCT (ST_UpperLeftX(rast), ST_UpperLeftY(rast)))) bad;
        check_name := 'rain.chirps_daily.tiles_per_day';
        status := CASE WHEN v_int = 0 THEN 'PASS' ELSE 'FAIL' END;
        detail := format('%s dates with missing or duplicated tiles', v_int);
        RETURN NEXT;

        SELECT count(*) INTO v_int FROM (
            SELECT obs_date, ST_SummaryStatsAgg(rast, 1, true) s FROM rain.chirps_daily GROUP BY obs_date) t
         WHERE (s).min < 0 OR (s).max > 500;
        check_name := 'rain.chirps_daily.value_range';
        status := CASE WHEN v_int = 0 THEN 'PASS' ELSE 'FAIL' END;
        detail := format('%s dates with values outside 0..500 mm/day', v_int);
        RETURN NEXT;
    END IF;

    -- 4. Climatology --------------------------------------------------------------------------
    IF NOT v_has_clim THEN
        check_name := 'rain.clim_p95_3d'; status := 'FAIL'; detail := 'table missing - build/load climatology';
        RETURN NEXT;
    ELSE
        SELECT count(*), string_agg(filename, ', ') INTO v_int, v_txt FROM rain.climatology_catalog;
        check_name := 'rain.climatology.catalog';
        status := CASE WHEN v_int > 0 THEN 'PASS' ELSE 'FAIL' END;
        detail := format('%s climatology raster(s): %s', v_int, coalesce(v_txt, 'none'));
        RETURN NEXT;

        IF v_has_daily THEN
            SELECT bool_and(ST_SameAlignment(c.rast, d.rast)) INTO v_bool
              FROM (SELECT DISTINCT ON (filename) rast FROM rain.clim_p95_3d ORDER BY filename, rid) c,
                   (SELECT rast FROM rain.chirps_daily ORDER BY rid LIMIT 1) d;
            check_name := 'rain.climatology.alignment';
            status := CASE WHEN coalesce(v_bool, false) THEN 'PASS' ELSE 'FAIL' END;
            detail := 'climatology on the same grid as daily rainfall: ' || coalesce(v_bool::text, 'unknown');
            RETURN NEXT;

            SELECT count(*) INTO v_int FROM (
                SELECT DISTINCT obs_date FROM rain.chirps_daily
                 WHERE ews.pick_climatology(obs_date, obs_date) IS NULL) t;
            check_name := 'rain.climatology.coverage';
            status := CASE WHEN v_int = 0 THEN 'PASS' ELSE 'WARN' END;
            detail := format('%s loaded dates outside every climatology seasonal window', v_int);
            RETURN NEXT;
        END IF;
    END IF;

    -- 5. Results ------------------------------------------------------------------------------
    SELECT count(*) INTO v_int FROM (
        SELECT end_date FROM ews.district_exceedance GROUP BY end_date
        HAVING count(*) <> (SELECT count(*) FROM ref.district)) t;
    SELECT count(DISTINCT end_date) INTO v_int2 FROM ews.district_exceedance;
    check_name := 'ews.exceedance.completeness';
    status := CASE WHEN v_int2 = 0 THEN 'WARN' WHEN v_int = 0 THEN 'PASS' ELSE 'WARN' END;
    detail := format('%s result dates, %s without a row for every district', v_int2, v_int);
    RETURN NEXT;

    IF v_has_daily THEN
        SELECT count(*) INTO v_int FROM (
            SELECT DISTINCT obs_date FROM rain.chirps_daily d
             WHERE NOT EXISTS (SELECT 1 FROM ews.district_raster_stats s
                                WHERE s.product = 'chirps' AND s.obs_date = d.obs_date)) t;
        check_name := 'ews.stats.coverage';
        status := CASE WHEN v_int = 0 THEN 'PASS' ELSE 'WARN' END;
        detail := format('%s loaded dates without district statistics (run compute)', v_int);
        RETURN NEXT;
    END IF;

    -- 6. CAP messages -------------------------------------------------------------------------
    SELECT count(*), count(*) FILTER (WHERE NOT validated) INTO v_int, v_int2 FROM ews.cap_message;
    check_name := 'ews.cap.validated';
    status := CASE WHEN v_int2 = 0 THEN 'PASS' ELSE 'FAIL' END;
    detail := format('%s CAP messages stored, %s failed validation', v_int, v_int2);
    RETURN NEXT;

    SELECT count(*) INTO v_int FROM ews.cap_message m
     WHERE m.msg_type IN ('Update', 'Cancel')
       AND (m.references_ids IS NULL OR NOT EXISTS (
            SELECT 1 FROM ews.cap_message p WHERE strpos(m.references_ids, ',' || p.identifier || ',') > 0));
    check_name := 'ews.cap.references';
    status := CASE WHEN v_int = 0 THEN 'PASS' ELSE 'FAIL' END;
    detail := format('%s Update/Cancel messages whose references do not resolve', v_int);
    RETURN NEXT;

    -- 7. Operations ---------------------------------------------------------------------------
    SELECT max(logged_at) INTO v_txt FROM ops.operations_log
     WHERE category = 'BACKUP' AND severity = 'INFO' AND details ->> 'verified' = 'true';
    check_name := 'ops.backup.recent';
    status := CASE WHEN v_txt IS NULL THEN 'WARN'
                   WHEN v_txt::timestamptz < now() - interval '7 days' THEN 'WARN' ELSE 'PASS' END;
    detail := 'last verified backup: ' || coalesce(v_txt, 'never');
    RETURN NEXT;

    SELECT count(*) INTO v_int FROM ops.pipeline_run r
     WHERE r.status = 'running' AND r.started_at < now() - interval '6 hours';
    check_name := 'ops.runs.stale';
    status := CASE WHEN v_int = 0 THEN 'PASS' ELSE 'WARN' END;
    detail := format('%s runs still marked running after 6 h', v_int);
    RETURN NEXT;
END $$;
