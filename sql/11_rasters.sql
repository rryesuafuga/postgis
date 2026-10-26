-- Post-load registration for tables written by raster2pgsql.
--
-- raster2pgsql -F stores the source file name in a "filename" column. These functions derive
-- typed columns from it and add the btree indexes the queries need. raster2pgsql -I has
-- already built the GiST index on ST_ConvexHull(rast); -C has added the raster constraints
-- (SRID, scale, block size, alignment, extent, nodata) that make a mis-gridded file fail to load.

-- Daily products: file names carry the date as YYYY.MM.DD (CHIRPS convention).
CREATE OR REPLACE FUNCTION rain.register_daily_table(p_table regclass)
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_n integer;
BEGIN
    EXECUTE format('ALTER TABLE %s ADD COLUMN IF NOT EXISTS obs_date date', p_table);
    EXECUTE format('ALTER TABLE %s ADD COLUMN IF NOT EXISTS product_source text', p_table);
    EXECUTE format($q$
        UPDATE %s SET
            obs_date = to_date(substring(filename FROM '(\d{4}\.\d{2}\.\d{2})'), 'YYYY.MM.DD'),
            product_source = CASE
                WHEN filename ILIKE 'synthetic%%' THEN 'SYNTHETIC'
                WHEN filename ILIKE 'chirps-v2.0%%' THEN 'CHIRPS v2.0'
                ELSE split_part(filename, '.', 1) END
        WHERE obs_date IS NULL$q$, p_table);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %s (obs_date)',
                   replace(p_table::text, '.', '_') || '_obs_date_idx', p_table);
    EXECUTE format('ANALYZE %s', p_table);
    RETURN v_n;
END $$;

-- Climatology rasters: clim_<stat>_<N>d_<y0>-<y1>_<MMDD>-<MMDD>.tif
--   band 1 = percentile of N-day totals, band 2 = mean of N-day totals.
CREATE TABLE IF NOT EXISTS rain.climatology_catalog (
    filename      text PRIMARY KEY,
    stat          text     NOT NULL,     -- e.g. p95
    accum_days    smallint NOT NULL,
    base_start    smallint NOT NULL,
    base_end      smallint NOT NULL,
    window_start  char(4)  NOT NULL,     -- MMDD
    window_end    char(4)  NOT NULL,     -- MMDD
    tiles         integer  NOT NULL,
    product_source text,
    registered_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION rain.register_climatology()
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_n integer;
BEGIN
    DELETE FROM rain.climatology_catalog;
    INSERT INTO rain.climatology_catalog
        (filename, stat, accum_days, base_start, base_end, window_start, window_end, tiles, product_source)
    SELECT c.filename, m[1], m[2]::smallint, m[3]::smallint, m[4]::smallint, m[5], m[6], count(*),
           CASE WHEN c.filename ILIKE '%synthetic%' THEN 'SYNTHETIC' ELSE 'CHIRPS v2.0' END
      FROM rain.clim_p95_3d c,
           LATERAL regexp_match(c.filename, 'clim_(p\d+)_(\d+)d_(\d{4})-(\d{4})_(\d{4})-(\d{4})') m
     GROUP BY c.filename, m;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    CREATE INDEX IF NOT EXISTS clim_p95_3d_filename_idx ON rain.clim_p95_3d (filename);
    ANALYZE rain.clim_p95_3d;
    RETURN v_n;
END $$;
