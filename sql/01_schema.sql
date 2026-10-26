-- Schema for the Uganda rainfall early-warning demo.
--   ref   administrative reference layers (districts)
--   rain  gridded rainfall loaded with raster2pgsql (tables are created by the loader)
--   ews   analysis results, trigger rules and CAP messages
--   ops   pipeline runs and the operations log
-- Idempotent: safe to re-run.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_raster;

-- PostGIS ships with every GDAL raster driver disabled. ST_AsPNG (map overlay) and ST_AsGDALRaster
-- exports need PNG/GTiff; enabled for new sessions on this database (superuser required).
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I SET postgis.gdal_enabled_drivers = %L', current_database(), 'PNG GTiff');
EXCEPTION WHEN insufficient_privilege THEN
    RAISE WARNING 'not superuser: set postgis.gdal_enabled_drivers = ''PNG GTiff'' to enable raster exports';
END $$;

CREATE SCHEMA IF NOT EXISTS ref;
CREATE SCHEMA IF NOT EXISTS rain;
CREATE SCHEMA IF NOT EXISTS ews;
CREATE SCHEMA IF NOT EXISTS ops;

------------------------------------------------------------------------------
-- ops: runs and operations log (created first: everything else logs into it)
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.pipeline_run (
    run_id       bigserial PRIMARY KEY,
    command      text        NOT NULL,
    params       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    status       text        NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running', 'success', 'partial', 'failed')),
    rows_written integer,
    message      text,
    operator     text        NOT NULL DEFAULT current_user,
    host         text        DEFAULT inet_client_addr()::text,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);

CREATE TABLE IF NOT EXISTS ops.operations_log (
    log_id    bigserial PRIMARY KEY,
    logged_at timestamptz NOT NULL DEFAULT now(),
    run_id    bigint REFERENCES ops.pipeline_run (run_id) ON DELETE SET NULL,
    category  text NOT NULL CHECK (category IN (
                  'STATUS',      -- routine system status / health check
                  'INGEST',      -- data loaded or re-ingested
                  'PROCESS',     -- analysis run
                  'EXCEEDANCE',  -- threshold exceedance detected
                  'ALERT',       -- CAP message prepared / issued / updated / cancelled
                  'FAULT',       -- fault encountered
                  'ACTION',      -- remedial or operator action taken
                  'ESCALATION',  -- fault escalated (ICPAC / service provider)
                  'BACKUP',      -- backup taken and verified
                  'INTEGRITY')), -- integrity check result
    component text NOT NULL,     -- e.g. db, chirps-feed, districts, climatology, cap, backup
    severity  text NOT NULL DEFAULT 'INFO' CHECK (severity IN ('INFO', 'WARN', 'ERROR')),
    message   text NOT NULL,
    details   jsonb,
    operator  text NOT NULL DEFAULT current_user
);
CREATE INDEX IF NOT EXISTS operations_log_time_idx ON ops.operations_log (logged_at DESC);
CREATE INDEX IF NOT EXISTS operations_log_cat_idx  ON ops.operations_log (category, logged_at DESC);

CREATE OR REPLACE FUNCTION ops.log(
    p_category  text,
    p_component text,
    p_severity  text,
    p_message   text,
    p_details   jsonb  DEFAULT NULL,
    p_run_id    bigint DEFAULT NULL)
RETURNS bigint LANGUAGE sql AS $$
    INSERT INTO ops.operations_log (category, component, severity, message, details, run_id)
    VALUES (upper(p_category), p_component, upper(p_severity), p_message, p_details, p_run_id)
    RETURNING log_id;
$$;

------------------------------------------------------------------------------
-- ref: districts (normalised from the ogr2ogr staging table ref.district_src)
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref.district (
    district_id serial PRIMARY KEY,
    pcode       text NOT NULL UNIQUE,
    name        text NOT NULL,
    region      text,
    source      text NOT NULL,
    code_scheme text NOT NULL,       -- geocode valueName used in CAP <geocode>
    area_km2    double precision,
    geom        geometry(MultiPolygon, 4326) NOT NULL,
    loaded_at   timestamptz NOT NULL DEFAULT now()
);
-- Explicit GiST index (ogr2ogr is told not to build one on the staging table).
CREATE INDEX IF NOT EXISTS district_geom_gist ON ref.district USING gist (geom);

------------------------------------------------------------------------------
-- ews: trigger configuration
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ews.alert_level (
    level        text PRIMARY KEY,
    rank         smallint NOT NULL UNIQUE,
    cap_severity text NOT NULL CHECK (cap_severity IN ('Extreme', 'Severe', 'Moderate', 'Minor', 'Unknown')),
    cap_urgency  text NOT NULL CHECK (cap_urgency IN ('Immediate', 'Expected', 'Future', 'Past', 'Unknown')),
    cap_certainty text NOT NULL CHECK (cap_certainty IN ('Observed', 'Likely', 'Possible', 'Unlikely', 'Unknown')),
    cap_response text NOT NULL,
    colour       text NOT NULL
);

CREATE TABLE IF NOT EXISTS ews.trigger_rule (
    rule_code   text PRIMARY KEY,
    description text NOT NULL,
    accum_days  smallint NOT NULL DEFAULT 3,
    metric      text NOT NULL CHECK (metric IN (
                    'mean_over_p95',      -- district-mean accumulation > threshold x district-mean P95
                    'area_frac_over_p95', -- fraction of district pixels whose accumulation > pixel P95
                    'max_mm',             -- wettest pixel accumulation >= threshold mm
                    'mean_mm')),          -- district-mean accumulation >= threshold mm
    threshold   real NOT NULL,
    -- Absolute floor on the district-mean total: a statistically unusual but small total
    -- (e.g. 35 mm in a dry zone) should not raise a warning on its own.
    min_accum_mm real NOT NULL DEFAULT 0,
    level       text NOT NULL REFERENCES ews.alert_level (level),
    active      boolean NOT NULL DEFAULT true,
    note        text
);

------------------------------------------------------------------------------
-- ews: results
------------------------------------------------------------------------------
-- Zonal statistics of any daily raster product per district (step 3).
CREATE TABLE IF NOT EXISTS ews.district_raster_stats (
    district_id integer NOT NULL REFERENCES ref.district (district_id) ON DELETE CASCADE,
    product     text    NOT NULL,          -- 'chirps' or an auxiliary layer name
    obs_date    date    NOT NULL,
    mean_mm     real,
    max_mm      real,
    min_mm      real,
    stddev_mm   real,
    pixel_count integer NOT NULL,
    method      text    NOT NULL CHECK (method IN ('clip', 'point')),
    run_id      bigint REFERENCES ops.pipeline_run (run_id) ON DELETE SET NULL,
    computed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (district_id, product, obs_date)
);
CREATE INDEX IF NOT EXISTS district_raster_stats_date_idx ON ews.district_raster_stats (product, obs_date);

-- Accumulation vs climatology and trigger outcome per district and window end date (step 4/5).
CREATE TABLE IF NOT EXISTS ews.district_exceedance (
    district_id        integer  NOT NULL REFERENCES ref.district (district_id) ON DELETE CASCADE,
    end_date           date     NOT NULL,
    accum_days         smallint NOT NULL DEFAULT 3,
    accum_mean_mm      real,
    accum_max_mm       real,
    p95_mean_mm        real,              -- district mean of the pixel P95 climatology
    clim_mean_mm       real,              -- district mean of the climatological mean accumulation
    ratio_to_p95       real,              -- accum_mean_mm / p95_mean_mm
    frac_area_above_p95 real,             -- 0..1
    pixel_count        integer,
    method             text,
    climatology        text NOT NULL,     -- climatology raster filename used
    triggered_rules    text[] NOT NULL DEFAULT '{}',
    alert_level        text NOT NULL DEFAULT 'none' REFERENCES ews.alert_level (level),
    run_id             bigint REFERENCES ops.pipeline_run (run_id) ON DELETE SET NULL,
    computed_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (district_id, end_date, accum_days)
);
CREATE INDEX IF NOT EXISTS district_exceedance_flag_idx
    ON ews.district_exceedance (end_date, alert_level) WHERE alert_level <> 'none';

------------------------------------------------------------------------------
-- ews: CAP messages (lifecycle: Alert -> Update(s) -> Cancel, linked by references)
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ews.cap_message (
    identifier        text PRIMARY KEY,
    sender            text NOT NULL,
    sent              timestamptz NOT NULL,
    status            text NOT NULL CHECK (status IN ('Actual', 'Exercise', 'System', 'Test', 'Draft')),
    msg_type          text NOT NULL CHECK (msg_type IN ('Alert', 'Update', 'Cancel', 'Ack', 'Error')),
    scope             text NOT NULL,
    references_ids    text,
    district_id       integer REFERENCES ref.district (district_id) ON DELETE SET NULL,  -- keep the audit trail
    end_date          date,
    alert_level       text REFERENCES ews.alert_level (level),
    event             text,
    urgency           text,
    severity          text,
    certainty         text,
    expires           timestamptz,
    headline          text,
    approved_by       text,
    validated         boolean NOT NULL,
    validation_errors text[] NOT NULL DEFAULT '{}',
    xml               xml NOT NULL,
    file_path         text,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cap_message_district_idx ON ews.cap_message (district_id, sent DESC);

-- Every CAP message stored is also an operations-log entry.
CREATE OR REPLACE FUNCTION ews.cap_message_log() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM ops.log('ALERT', 'cap',
        CASE WHEN NEW.validated THEN 'INFO' ELSE 'WARN' END,
        format('CAP %s %s (%s) for %s: %s', NEW.msg_type, NEW.identifier, NEW.status,
               coalesce((SELECT name FROM ref.district WHERE district_id = NEW.district_id), 'n/a'),
               coalesce(NEW.headline, '')),
        jsonb_build_object('identifier', NEW.identifier, 'status', NEW.status,
                           'msg_type', NEW.msg_type, 'references', NEW.references_ids,
                           'severity', NEW.severity, 'validated', NEW.validated,
                           'approved_by', NEW.approved_by, 'file', NEW.file_path));
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS cap_message_log_trg ON ews.cap_message;
CREATE TRIGGER cap_message_log_trg AFTER INSERT ON ews.cap_message
    FOR EACH ROW EXECUTE FUNCTION ews.cap_message_log();
