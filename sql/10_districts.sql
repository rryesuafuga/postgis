-- Normalise the ogr2ogr staging table ref.district_src into ref.district.
--
-- ogr2ogr launders field names to lower case, so the usual source attributes are:
--   OCHA COD-AB (HDX)   adm2_en | adm2_name, adm2_pcode, adm1_en | adm1_name   (admin 2 = district)
--   GADM 4.x            name_N, gid_N for the chosen level
--   synthetic fixture   name, pcode, region
-- Columns are auto-detected unless passed explicitly. Districts are upserted by code so
-- district_id (and the results that reference it) survive a boundary refresh.

CREATE OR REPLACE FUNCTION ref.normalize_districts(
    p_source      text,
    p_code_scheme text,
    p_name_col    text DEFAULT NULL,
    p_code_col    text DEFAULT NULL,
    p_region_col  text DEFAULT NULL)
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
          FROM unnest(ARRAY['adm2_en', 'adm2_name', 'name_2', 'name_1', 'district', 'name']) WITH ORDINALITY t(c, o)
         WHERE c = ANY (v_cols) ORDER BY o LIMIT 1;
    END IF;
    IF v_code IS NULL THEN
        SELECT c INTO v_code
          FROM unnest(ARRAY['adm2_pcode', 'gid_2', 'gid_1', 'pcode', 'code']) WITH ORDINALITY t(c, o)
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
    EXECUTE format($q$
        WITH src AS (
            SELECT %1$I::text AS code,
                   min(%2$I::text) AS name,
                   min(%3$s) AS region,
                   ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Union(ST_Force2D(%4$I))), 3)) AS geom
              FROM ref.district_src
             WHERE %1$I IS NOT NULL
             GROUP BY 1
        )
        INSERT INTO ref.district AS d (pcode, name, region, source, code_scheme, area_km2, geom, loaded_at)
        SELECT code, name, region, $1, $2, ST_Area(geom::geography) / 1e6, geom, now()
          FROM src
        ON CONFLICT (pcode) DO UPDATE SET
            name = EXCLUDED.name, region = EXCLUDED.region, source = EXCLUDED.source,
            code_scheme = EXCLUDED.code_scheme, area_km2 = EXCLUDED.area_km2,
            geom = EXCLUDED.geom, loaded_at = EXCLUDED.loaded_at
        $q$,
        v_code, v_name,
        CASE WHEN v_region IS NULL THEN 'NULL::text' ELSE format('%I::text', v_region) END,
        v_geom)
    USING p_source, p_code_scheme;
    GET DIAGNOSTICS v_n = ROW_COUNT;

    -- Districts no longer in the source are removed (their results cascade).
    EXECUTE format('DELETE FROM ref.district d WHERE NOT EXISTS
                      (SELECT 1 FROM ref.district_src s WHERE s.%I::text = d.pcode)', v_code);
    GET DIAGNOSTICS v_gone = ROW_COUNT;

    PERFORM ops.log('INGEST', 'districts', 'INFO',
        format('Loaded %s districts from %s (%s removed)', v_n, p_source, v_gone),
        jsonb_build_object('name_col', v_name, 'code_col', v_code, 'region_col', v_region,
                           'code_scheme', p_code_scheme, 'removed', v_gone));
    RETURN v_n;
END $$;
