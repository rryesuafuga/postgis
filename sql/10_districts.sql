-- Normalise the ogr2ogr staging table ref.district_src into ref.district.
--
-- ogr2ogr launders field names to lower case, so the usual source attributes are:
--   OCHA COD-AB (HDX)   adm2_en | adm2_name, adm2_pcode, adm1_en | adm1_name   (admin 2 = district)
--   GADM 4.x            name_N, gid_N for the chosen level
--   geoBoundaries       shapename, shapeid (no parent-region column)
-- Columns are auto-detected unless passed explicitly. Districts are upserted by code so
-- district_id (and the results that reference it) survive a boundary refresh.
-- Data standards applied on load, each recorded in the INGEST log entry:
--   * units whose name matches p_exclude (default: water bodies, "Lake ...") are left out;
--   * known misspellings in a source are corrected through ref.district_name_fix.

CREATE TABLE IF NOT EXISTS ref.district_name_fix (
    source_name text PRIMARY KEY,
    name        text NOT NULL,
    note        text NOT NULL
);
INSERT INTO ref.district_name_fix (source_name, name, note) VALUES
    ('Kalanga',    'Kalangala',  'geoBoundaries ADM3 spelling; the Ssese Islands district'),
    ('Kitagwenga', 'Kitagwenda', 'geoBoundaries ADM3 spelling; district created from Kamwenge in 2019')
ON CONFLICT (source_name) DO UPDATE SET name = EXCLUDED.name, note = EXCLUDED.note;

DROP FUNCTION IF EXISTS ref.normalize_districts(text, text, text, text, text);

CREATE OR REPLACE FUNCTION ref.normalize_districts(
    p_source      text,
    p_code_scheme text,
    p_name_col    text DEFAULT NULL,
    p_code_col    text DEFAULT NULL,
    p_region_col  text DEFAULT NULL,
    p_exclude     text DEFAULT '^lake ')
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_cols   text[];
    v_name   text := nullif(p_name_col, '');
    v_code   text := nullif(p_code_col, '');
    v_region text := nullif(p_region_col, '');
    v_geom   text;
    v_n      integer;
    v_gone   integer;
    v_excl   text[];
    v_fixed  text[];
BEGIN
    SELECT array_agg(column_name::text) INTO v_cols
      FROM information_schema.columns
     WHERE table_schema = 'ref' AND table_name = 'district_src';
    IF v_cols IS NULL THEN
        RAISE EXCEPTION 'ref.district_src not found: run scripts/10_load_districts.sh first';
    END IF;

    SELECT f_geometry_column INTO v_geom FROM geometry_columns
     WHERE f_table_schema = 'ref' AND f_table_name = 'district_src' LIMIT 1;

    IF v_name IS NULL THEN
        SELECT c INTO v_name
          FROM unnest(ARRAY['adm2_en', 'adm2_name', 'shapename', 'name_2', 'name_1', 'district', 'name']) WITH ORDINALITY t(c, o)
         WHERE c = ANY (v_cols) ORDER BY o LIMIT 1;
    END IF;
    IF v_code IS NULL THEN
        SELECT c INTO v_code
          FROM unnest(ARRAY['adm2_pcode', 'shapeid', 'gid_2', 'gid_1', 'pcode', 'code']) WITH ORDINALITY t(c, o)
         WHERE c = ANY (v_cols) ORDER BY o LIMIT 1;
    END IF;
    IF v_region IS NULL THEN
        SELECT c INTO v_region
          FROM unnest(ARRAY['adm1_en', 'adm1_name', 'region',
                            CASE WHEN v_name = 'name_2' THEN 'name_1' END]) WITH ORDINALITY t(c, o)
         WHERE c = ANY (v_cols) ORDER BY o LIMIT 1;
    END IF;

    IF v_name IS NULL OR v_code IS NULL OR v_geom IS NULL THEN
        RAISE EXCEPTION 'Could not detect name/code/geometry columns in ref.district_src (columns: %). '
                        'Set EWS_DISTRICT_NAME_FIELD / EWS_DISTRICT_CODE_FIELD.', v_cols;
    END IF;
    RAISE NOTICE 'Normalising districts: name=%, code=%, region=%, geom=%', v_name, v_code, v_region, v_geom;

    -- Multi-row districts (split parts) are dissolved; geometries repaired and forced to MultiPolygon.
    DROP TABLE IF EXISTS pg_temp.district_load;
    EXECUTE format($q$
        CREATE TEMP TABLE district_load AS
        WITH raw AS (
            SELECT %1$I::text AS code,
                   min(%2$I::text) AS src_name,
                   min(%3$s) AS region,
                   ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Union(ST_Force2D(%4$I))), 3)) AS geom
              FROM ref.district_src
             WHERE %1$I IS NOT NULL
             GROUP BY 1
        )
        SELECT r.code, r.src_name, coalesce(f.name, r.src_name) AS name, r.region, r.geom
          FROM raw r LEFT JOIN ref.district_name_fix f ON f.source_name = r.src_name
        $q$,
        v_code, v_name,
        CASE WHEN v_region IS NULL THEN 'NULL::text' ELSE format('%I::text', v_region) END,
        v_geom);

    SELECT array_agg(src_name ORDER BY src_name) INTO v_excl
      FROM district_load WHERE nullif(p_exclude, '') IS NOT NULL AND src_name ~* p_exclude;
    DELETE FROM district_load WHERE nullif(p_exclude, '') IS NOT NULL AND src_name ~* p_exclude;
    SELECT array_agg(src_name || ' -> ' || name ORDER BY src_name) INTO v_fixed
      FROM district_load WHERE name <> src_name;

    INSERT INTO ref.district AS d (pcode, name, region, source, code_scheme, area_km2, geom, loaded_at)
    SELECT code, name, region, p_source, p_code_scheme, ST_Area(geom::geography) / 1e6, geom, now()
      FROM district_load
    ON CONFLICT (pcode) DO UPDATE SET
        name = EXCLUDED.name, region = EXCLUDED.region, source = EXCLUDED.source,
        code_scheme = EXCLUDED.code_scheme, area_km2 = EXCLUDED.area_km2,
        geom = EXCLUDED.geom, loaded_at = EXCLUDED.loaded_at;
    GET DIAGNOSTICS v_n = ROW_COUNT;

    -- Districts no longer in the (filtered) source are removed (their results cascade).
    DELETE FROM ref.district d WHERE NOT EXISTS (SELECT 1 FROM district_load s WHERE s.code = d.pcode);
    GET DIAGNOSTICS v_gone = ROW_COUNT;
    DROP TABLE district_load;

    PERFORM ops.log('INGEST', 'districts', 'INFO',
        format('Loaded %s districts from %s (%s removed, %s excluded, %s names corrected)',
               v_n, p_source, v_gone, coalesce(cardinality(v_excl), 0), coalesce(cardinality(v_fixed), 0)),
        jsonb_build_object('name_col', v_name, 'code_col', v_code, 'region_col', v_region,
                           'code_scheme', p_code_scheme, 'removed', v_gone,
                           'excluded', v_excl, 'name_fixes', v_fixed));
    RETURN v_n;
END $$;
