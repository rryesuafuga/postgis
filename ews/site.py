"""Export the demo database to a self-contained static page (docs/index.html, for GitHub Pages).

Everything on the page is read from PostGIS at export time: district outlines, the exceedance
results for every 3-day window, daily district means, the rainfall grid of each window rendered
inside PostGIS (ST_Union -> ST_Clip -> ST_ColorMap -> ST_AsPNG), the CAP messages, the integrity
checks and the operations log. The page needs no server and no external scripts.

    make site            (or: EWS_DATA_DIR=data/synthetic PGDATABASE=ews_demo python -m ews site)
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from . import cap, config, grid
from .db import connect

TEMPLATE = Path(__file__).parent / "site_template.html"
LEVEL_CODE = {"none": 0, "watch": 1, "warning": 2}
# Same colour scale as the app's raster overlay; ST_ColorMap wants entries from high to low.
COLORMAP = """200 13 54 107 245
120 24 79 149 235
80 37 106 191 225
60 57 135 229 210
40 109 167 236 195
25 158 197 244 175
10 205 226 251 150
0 255 255 255 0
nv 0 0 0 0"""


class Projector:
    """Equirectangular projection (scaled by cos of the mean latitude) into SVG units."""

    def __init__(self, bounds: tuple[float, float, float, float], width: float = 640.0, pad: float = 0.04):
        w, s, e, n = bounds
        self.w, self.s, self.e, self.n = w - pad, s - pad, e + pad, n + pad
        self.kx = math.cos(math.radians((self.s + self.n) / 2))
        self.scale = width / ((self.e - self.w) * self.kx)
        self.width = width
        self.height = (self.n - self.s) * self.scale

    def xy(self, lon: float, lat: float) -> tuple[float, float]:
        return (lon - self.w) * self.kx * self.scale, (self.n - lat) * self.scale

    def path(self, geojson: dict) -> str:
        polys = geojson["coordinates"] if geojson["type"] == "MultiPolygon" else [geojson["coordinates"]]
        out = []
        for poly in polys:
            for ring in poly:
                pts = [self.xy(lon, lat) for lon, lat in ring]
                out.append("M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + "Z")
        return "".join(out)


def _rows(conn, sql: str, **p) -> list[dict]:
    return [dict(r) for r in conn.execute(text(sql), p).mappings().all()]


def collect() -> dict:
    with connect() as conn:
        b = conn.execute(text("SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) "
                              "FROM (SELECT ST_Extent(geom) AS e FROM ref.district) t")).one()
        proj = Projector(tuple(b))
        districts = _rows(conn, """
            SELECT district_id, name, pcode, region, round(area_km2) AS area_km2,
                   ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, 0.003), 5) AS gj
              FROM ref.district ORDER BY name""")
        index = {d["district_id"]: i for i, d in enumerate(districts)}
        dates = [r["end_date"] for r in _rows(conn, """
            SELECT DISTINCT end_date FROM ews.district_exceedance WHERE accum_days = 3 ORDER BY 1""")]
        days = [r["obs_date"] for r in _rows(conn, """
            SELECT DISTINCT obs_date FROM ews.district_raster_stats WHERE product = 'chirps' ORDER BY 1""")]

        exc = {d.isoformat(): [None] * len(districts) for d in dates}
        for r in _rows(conn, """SELECT district_id, end_date, accum_mean_mm, accum_max_mm, p95_mean_mm,
                                       ratio_to_p95, frac_area_above_p95, alert_level,
                                       array_to_string(triggered_rules, ' ') AS rules
                                  FROM ews.district_exceedance WHERE accum_days = 3"""):
            exc[r["end_date"].isoformat()][index[r["district_id"]]] = [
                round(r["accum_mean_mm"], 1), round(r["accum_max_mm"], 1), round(r["p95_mean_mm"], 1),
                round(r["ratio_to_p95"] or 0, 2), round(r["frac_area_above_p95"], 2),
                LEVEL_CODE[r["alert_level"]], r["rules"]]

        day_pos = {d: i for i, d in enumerate(days)}
        daily = {p: [[None] * len(days) for _ in districts] for p in ("chirps", "aux")}
        aux_name = None
        for r in _rows(conn, "SELECT district_id, product, obs_date, mean_mm FROM ews.district_raster_stats"):
            key = "chirps" if r["product"] == "chirps" else "aux"
            if key == "aux":
                aux_name = r["product"]
            if r["obs_date"] in day_pos:
                daily[key][index[r["district_id"]]][day_pos[r["obs_date"]]] = round(r["mean_mm"], 1)

        g = (grid.WEST, grid.SOUTH, grid.EAST, grid.NORTH)
        x0, y0 = proj.xy(g[0], g[3])
        x1, y1 = proj.xy(g[2], g[1])
        rasters = {}
        for d in dates:
            png = conn.execute(text(f"""
                SELECT encode(ST_AsPNG(ST_ColorMap(ST_Clip(ST_Union(rast),
                                       ST_MakeEnvelope(:w, :s, :e, :n, 4326)), 1, '{COLORMAP}', 'INTERPOLATE')),
                              'base64')
                  FROM rain.chirps_accum WHERE end_date = :d AND accum_days = 3"""),
                {"d": d, "w": g[0], "s": g[1], "e": g[2], "n": g[3]}).scalar_one()
            if png:
                rasters[d.isoformat()] = "data:image/png;base64," + png.replace("\n", "")

        cap_rows = _rows(conn, """
            SELECT to_char(m.sent AT TIME ZONE 'Africa/Kampala', 'YYYY-MM-DD HH24:MI') AS sent,
                   m.msg_type, m.status, d.name AS district, m.end_date, m.severity, m.urgency,
                   m.certainty, m.identifier, m.references_ids, m.xml::text AS xml
              FROM ews.cap_message m LEFT JOIN ref.district d USING (district_id)
             ORDER BY m.sent DESC, m.created_at DESC""")
        integrity = _rows(conn, "SELECT check_name, status, detail FROM ops.integrity_checks()")
        oplog = _rows(conn, """
            SELECT to_char(logged_at AT TIME ZONE 'Africa/Kampala', 'YYYY-MM-DD HH24:MI:SS') AS t,
                   category, severity, component, message
              FROM ops.operations_log ORDER BY log_id DESC LIMIT 40""")
        rules = _rows(conn, "SELECT rule_code, description, level, threshold, min_accum_mm FROM ews.trigger_rule "
                            "WHERE active ORDER BY level DESC, rule_code")
        meta = conn.execute(text("""
            SELECT (SELECT string_agg(DISTINCT source, ', ') FROM ref.district) AS district_source,
                   (SELECT string_agg(DISTINCT product_source, ', ') FROM rain.chirps_daily) AS rain_source,
                   (SELECT string_agg(filename, ', ') FROM rain.climatology_catalog) AS climatology,
                   (SELECT count(*) FROM rain.chirps_daily) AS raster_tiles,
                   postgis_lib_version() AS postgis, current_setting('server_version') AS postgres,
                   (SELECT to_char(max(logged_at) AT TIME ZONE 'Africa/Kampala', 'DD Mon HH24:MI')
                      FROM ops.operations_log
                     WHERE category = 'BACKUP' AND details ->> 'verified' = 'true') AS last_backup
            """)).mappings().one()

    synthetic = "SYNTHETIC" in str(meta["rain_source"]).upper()
    return {
        "meta": {**dict(meta), "synthetic_rain": synthetic, "aux_name": aux_name,
                 "exported": datetime.now(cap.EAT).strftime("%d %b %Y %H:%M EAT"),
                 "repo": "https://github.com/rryesuafuga/postgis"},
        "view": {"w": round(proj.width, 1), "h": round(proj.height, 1)},
        "grid_box": [round(x0, 1), round(y0, 1), round(x1 - x0, 1), round(y1 - y0, 1)],
        "districts": [{"name": d["name"], "code": d["pcode"], "km2": int(d["area_km2"] or 0),
                       "path": proj.path(json.loads(d["gj"]))} for d in districts],
        "dates": [d.isoformat() for d in dates],
        "days": [d.isoformat() for d in days],
        "exc": exc,
        "daily": daily,
        "rasters": rasters,
        "cap": [{**r, "end_date": r["end_date"].isoformat() if r["end_date"] else None} for r in cap_rows],
        "integrity": integrity,
        "oplog": oplog,
        "rules": rules,
    }


def export(out: Path | None = None) -> Path:
    out = out or config.ROOT / "docs" / "index.html"
    data = json.dumps(collect(), separators=(",", ":"), default=str).replace("</", "<\\/")
    html = TEMPLATE.read_text().replace("/*__DATA__*/{}", data)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    (out.parent / ".nojekyll").touch()
    return out
