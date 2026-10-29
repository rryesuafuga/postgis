"""Streamlit map of district rainfall exceedance, CAP messages and the operations log.

    streamlit run app/streamlit_app.py            (PG* environment variables / .env select the database)

The map uses pydeck, which ships inside Streamlit: district layers and the PostGIS-rendered
rainfall raster still draw when the basemap tile server is unreachable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ews import config  # noqa: E402

st.set_page_config(page_title="Uganda Rainfall EWS", page_icon=":cloud_with_rain:", layout="wide")

# Status palette (reserved meaning; always shown with a label) and a one-hue sequential ramp.
LEVEL_STYLE = {
    "warning": {"hex": "#d03b3b", "label": "Warning", "icon": "■"},
    "watch":   {"hex": "#fab219", "label": "Watch",   "icon": "▲"},
    "none":    {"hex": "#f0efec", "label": "No trigger", "icon": "·"},
}
BLUES = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
MM_BINS = [10, 25, 40, 60, 80, 120]                 # 3-day totals, mm
PCT_BINS = [5, 15, 30, 50, 70, 90]                  # % of district above pixel P95
RATIO_BINS = [0.5, 0.75, 0.9, 1.1, 1.5, 2.0]        # accumulation / P95, diverging around 1
RATIO_COLOURS = ["#256abf", "#6da7ec", "#b7d3f6", "#e1e0d9", "#f2b8b5", "#e66767", "#b52a2a"]
# ST_ColorMap entries must run from the highest value down (ascending order yields a blank image).
RASTER_COLORMAP = """200 13 54 107 245
120 24 79 149 235
80 37 106 191 225
60 57 135 229 210
40 109 167 236 195
25 158 197 244 175
10 205 226 251 150
0 255 255 255 0
nv 0 0 0 0"""

METRICS = {
    "Alert level": "alert_level",
    "3-day rainfall, district mean (mm)": "accum_mean_mm",
    "Ratio to 1991-2020 P95": "ratio_to_p95",
    "% of district above its P95": "pct_area",
}


@st.cache_resource
def engine():
    return create_engine(config.db_url(), pool_pre_ping=True)


@st.cache_data(ttl=60)
def q(sql: str, **params) -> pd.DataFrame:
    with engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


def rgba(hex_colour: str, alpha: int = 200) -> list[int]:
    h = hex_colour.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)] + [alpha]


def binned(value, bins, colours) -> str:
    if value is None or pd.isna(value):
        return "#ffffff"
    for edge, colour in zip(bins, colours, strict=False):    # colours has one more entry
        if value < edge:
            return colour
    return colours[len(bins)]


def legend_html(items: list[tuple[str, str]]) -> str:
    sw = "".join(
        f'<span style="display:inline-flex;align-items:center;margin-right:14px;font-size:0.85rem">'
        f'<span style="width:14px;height:14px;background:{c};border:1px solid rgba(11,11,11,.25);'
        f'border-radius:3px;margin-right:6px"></span>{label}</span>' for c, label in items)
    return f'<div style="margin:4px 0 10px 0">{sw}</div>'


# ------------------------------------------------------------------------------------------
# Data
# ------------------------------------------------------------------------------------------
try:
    dates = q("SELECT DISTINCT end_date FROM ews.district_exceedance WHERE accum_days = 3 ORDER BY 1")
except Exception as e:  # database down or schema missing: say so plainly
    st.error(f"Cannot read the database ({config.env('PGDATABASE', 'ews')} on "
             f"{config.env('PGHOST', 'localhost')}:{config.env('PGPORT', '5433')}): {e}")
    st.stop()
if dates.empty:
    st.warning("No results yet. Run `python -m ews compute` (or `make demo`) first.")
    st.stop()

meta = q("""SELECT (SELECT string_agg(DISTINCT source, ', ') FROM ref.district) AS district_source,
                   (SELECT string_agg(DISTINCT product_source, ', ') FROM rain.chirps_daily) AS rain_source,
                   (SELECT string_agg(filename, ', ') FROM rain.climatology_catalog) AS climatology""").iloc[0]
synthetic_boundaries = "SYNTHETIC" in str(meta.district_source).upper()
synthetic_rain = "SYNTHETIC" in str(meta.rain_source).upper()

# Open on the window of the latest CAP message, else on the window with the most flags.
alerted = q("""SELECT end_date FROM ews.cap_message
                WHERE status <> 'Draft' AND end_date IS NOT NULL ORDER BY sent DESC LIMIT 1""")
peak = q("""SELECT end_date FROM ews.district_exceedance x JOIN ews.alert_level l ON l.level = x.alert_level
             GROUP BY end_date ORDER BY sum(l.rank) DESC, end_date DESC LIMIT 1""")
if not alerted.empty and alerted.end_date.iloc[0] in set(dates.end_date):
    default_date = alerted.end_date.iloc[0]
else:
    default_date = peak.end_date.iloc[0] if not peak.empty else dates.end_date.iloc[-1]


def eat(ts) -> str:
    """A timestamp from the database, shown in East Africa Time."""
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t
    return t.tz_convert("Africa/Kampala").strftime("%d %b %H:%M")

# ------------------------------------------------------------------------------------------
# Sidebar
# ------------------------------------------------------------------------------------------
st.sidebar.title("Uganda rainfall EWS")
st.sidebar.caption(f"Database `{config.env('PGDATABASE', 'ews')}`")
day = st.sidebar.select_slider("3-day window ending", options=list(dates.end_date), value=default_date,
                               format_func=lambda d: d.strftime("%d %b %Y"))
metric_label = st.sidebar.radio("Colour districts by", list(METRICS))
metric = METRICS[metric_label]
show_raster = st.sidebar.toggle("Show 3-day rainfall grid (PostGIS ST_ColorMap)", value=False)
st.sidebar.divider()
st.sidebar.markdown(f"**Boundaries** {meta.district_source}  \n**Rainfall** {meta.rain_source}  \n"
                    f"**Climatology** `{meta.climatology}`")
st.sidebar.caption("Demonstration thresholds; warning authority rests with the designated national "
                   "approving authority.")

# ------------------------------------------------------------------------------------------
# Header + KPIs
# ------------------------------------------------------------------------------------------
st.title("District rainfall exceedance")
rain_label = "synthetic" if synthetic_rain else "CHIRPS"
st.caption(f"3-day {rain_label} rainfall ending {day:%d %B %Y} against the {rain_label} 1991-2020 95th "
           "percentile for the time of year, and fixed triggers")
if synthetic_rain and not synthetic_boundaries:
    st.warning("**SYNTHETIC RAINFALL.** The district boundaries are real, but the rainfall is generated "
               "to exercise the pipeline, with storms placed over Mt Elgon and the Rwenzori. Do not read "
               "it as conditions in Uganda.", icon="⚠️")
elif synthetic_rain or synthetic_boundaries:
    st.warning("**SYNTHETIC TEST DATA.** The boundaries and/or the rainfall are generated. Use this view "
               "to check the pipeline, not to read conditions in Uganda.", icon="⚠️")

res = q("""SELECT d.district_id, d.pcode, d.name, d.region, x.alert_level, x.accum_mean_mm, x.accum_max_mm,
                  x.p95_mean_mm, x.ratio_to_p95, 100 * x.frac_area_above_p95 AS pct_area,
                  array_to_string(x.triggered_rules, ', ') AS rules, x.method,
                  ST_AsGeoJSON(ST_SimplifyPreserveTopology(d.geom, 0.005), 4) AS geojson
             FROM ews.district_exceedance x JOIN ref.district d USING (district_id)
            WHERE x.end_date = :d AND x.accum_days = 3""", d=day)
cap_active = q("""SELECT count(*) AS n FROM ews.v_cap_lifecycle
                   WHERE is_latest AND msg_type <> 'Cancel' AND expires > now()""").n.iloc[0]
cap_window = q("""SELECT count(*) AS n FROM ews.cap_message
                   WHERE end_date = :d AND status <> 'Draft'""", d=day).n.iloc[0]
backup = q("""SELECT max(logged_at) AS t FROM ops.operations_log
               WHERE category = 'BACKUP' AND details ->> 'verified' = 'true'""").t.iloc[0]

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("■ Warning", int((res.alert_level == "warning").sum()), help="districts at warning level")
k2.metric("▲ Watch", int((res.alert_level == "watch").sum()), help="districts at watch level")
top = res.sort_values("accum_mean_mm", ascending=False).iloc[0]
k3.metric("Wettest district (3-day mean)", f"{top.accum_mean_mm:.0f} mm", top["name"], delta_color="off")
k4.metric("CAP messages, this window", int(cap_window),
          help=f"Issued for the 3-day window ending {day:%d %b %Y}; {int(cap_active)} active right now "
               "across all windows.")
k5.metric("Last verified backup (EAT)", "never" if pd.isna(backup) else eat(backup),
          help="pg_dump, then restored into a scratch database and checked (scripts/backup.sh)")

# ------------------------------------------------------------------------------------------
# Map
# ------------------------------------------------------------------------------------------
def fill_for(row) -> str:
    if metric == "alert_level":
        return LEVEL_STYLE[row.alert_level]["hex"]
    if metric == "accum_mean_mm":
        return binned(row.accum_mean_mm, MM_BINS, BLUES)
    if metric == "pct_area":
        return binned(row.pct_area, PCT_BINS, BLUES)
    return binned(row.ratio_to_p95, RATIO_BINS, RATIO_COLOURS)


features = []
for r in res.itertuples():
    style = LEVEL_STYLE[r.alert_level]
    features.append({
        "type": "Feature",
        "geometry": json.loads(r.geojson),
        "properties": {
            "name": r.name, "pcode": r.pcode,
            "level": f"{style['icon']} {style['label']}",
            "accum": f"{r.accum_mean_mm:.1f}", "max": f"{r.accum_max_mm:.0f}",
            "p95": f"{r.p95_mean_mm:.1f}", "ratio": f"{(r.ratio_to_p95 or 0):.2f}",
            "pct": f"{r.pct_area:.0f}", "rules": r.rules or "none",
            "fill": rgba(fill_for(r), 45 if show_raster else 205),
            "line": [11, 11, 11, 220] if r.alert_level != "none" else [82, 81, 78, 120],
            "width": 2.5 if r.alert_level == "warning" else 1.5 if r.alert_level == "watch" else 0.6,
        },
    })

def raster_layer(end_date) -> pdk.Layer | None:
    """3-day accumulation tiles mosaicked, colour-mapped and PNG-encoded inside PostGIS."""
    try:
        img = q(f"""SELECT encode(ST_AsPNG(ST_ColorMap(m.rast, 1, '{RASTER_COLORMAP}', 'INTERPOLATE')),
                                  'base64') AS png,
                           ST_XMin(e.box) AS w, ST_YMin(e.box) AS s, ST_XMax(e.box) AS e, ST_YMax(e.box) AS n
                      FROM (SELECT ST_Union(rast) AS rast FROM rain.chirps_accum
                             WHERE end_date = :d AND accum_days = 3) m,
                           LATERAL (SELECT ST_Envelope(m.rast) AS box) e""", d=end_date).iloc[0]
    except Exception as e:  # e.g. postgis.gdal_enabled_drivers does not include PNG
        st.sidebar.error(f"Raster overlay unavailable: {str(e).splitlines()[0]}")
        return None
    if not img.png:
        return None
    uri = "data:image/png;base64," + img.png.replace("\n", "")
    return pdk.Layer("BitmapLayer", image=f"'{uri}'",          # quoted: a literal, not an expression
                     bounds=[img.w, img.s, img.e, img.n], opacity=0.9)


layers = [raster_layer(day)] if show_raster else []
layers = [layer for layer in layers if layer is not None]
layers.append(pdk.Layer(
    "GeoJsonLayer", {"type": "FeatureCollection", "features": features}, pickable=True, stroked=True,
    filled=True, get_fill_color="properties.fill", get_line_color="properties.line",
    # pydeck evaluates strings as JS expressions: a literal string needs inner quotes.
    get_line_width="properties.width", line_width_units="'pixels'", auto_highlight=True,
    highlight_color=[11, 11, 11, 60]))

map_col, list_col = st.columns([3, 2])
with map_col:
    st.pydeck_chart(pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(latitude=1.35, longitude=32.35, zoom=5.9),
        map_style="light",
        tooltip={"html": "<b>{name}</b> ({pcode})<br/>{level}<br/>3-day mean <b>{accum} mm</b> "
                         "(P95 {p95} mm, ratio {ratio})<br/>wettest pixel {max} mm · {pct}% above P95"
                         "<br/><i>{rules}</i>",
                 "style": {"fontSize": "12px"}}), height=560)
    if metric == "alert_level":
        st.markdown(legend_html([(v["hex"], f"{v['icon']} {v['label']}") for v in LEVEL_STYLE.values()]),
                    unsafe_allow_html=True)
    elif metric == "ratio_to_p95":
        edges = ["<0.5", "0.5-0.75", "0.75-0.9", "0.9-1.1", "1.1-1.5", "1.5-2", "≥2"]
        st.markdown(legend_html(list(zip(RATIO_COLOURS, [f"{e}×" for e in edges], strict=True))),
                    unsafe_allow_html=True)
    else:
        bins = MM_BINS if metric == "accum_mean_mm" else PCT_BINS
        unit = " mm" if metric == "accum_mean_mm" else "%"
        edges = [f"<{bins[0]}"] + [f"{a}-{b}" for a, b in zip(bins, bins[1:], strict=False)] + [f"≥{bins[-1]}"]
        st.markdown(legend_html(list(zip(BLUES, [e + unit for e in edges], strict=True))), unsafe_allow_html=True)

with list_col:
    st.subheader("Flagged districts")
    flagged = res[res.alert_level != "none"].copy()
    if flagged.empty:
        st.info("No district meets a trigger for this window.")
    else:
        flagged["level"] = flagged.alert_level.map(lambda v: f"{LEVEL_STYLE[v]['icon']} {LEVEL_STYLE[v]['label']}")
        flagged["rank"] = flagged.alert_level.map({"warning": 2, "watch": 1})
        flagged = flagged.sort_values(["rank", "ratio_to_p95"], ascending=False)
        st.dataframe(
            flagged[["level", "name", "accum_mean_mm", "p95_mean_mm", "ratio_to_p95", "pct_area", "accum_max_mm", "rules"]],
            hide_index=True, use_container_width=True,
            column_config={
                "level": "Level", "name": "District",
                "accum_mean_mm": st.column_config.NumberColumn("3-day mean", format="%.0f mm"),
                "p95_mean_mm": st.column_config.NumberColumn("P95", format="%.0f mm"),
                "ratio_to_p95": st.column_config.NumberColumn("× P95", format="%.2f"),
                "pct_area": st.column_config.NumberColumn("% > P95", format="%.0f%%"),
                "accum_max_mm": st.column_config.NumberColumn("Max pixel", format="%.0f mm"),
                "rules": "Triggers",
            })
    st.caption("Small districts with no pixel centre inside the boundary are sampled at a point on the "
               "surface (method = point).")

# ------------------------------------------------------------------------------------------
# District detail
# ------------------------------------------------------------------------------------------
st.subheader("District time series")
order = (res.assign(rank=res.alert_level.map({"warning": 2, "watch": 1, "none": 0}))
         .sort_values(["rank", "ratio_to_p95"], ascending=False))
choice = st.selectbox("District", order.name.tolist(), index=0)
did = int(res.loc[res.name == choice, "district_id"].iloc[0])
ts = q("""SELECT s.obs_date AS date, s.product, s.mean_mm FROM ews.district_raster_stats s
           WHERE s.district_id = :d ORDER BY 1""", d=did)
ex = q("""SELECT end_date AS date, accum_mean_mm, p95_mean_mm, alert_level FROM ews.district_exceedance
           WHERE district_id = :d AND accum_days = 3 ORDER BY 1""", d=did)
if not ts.empty:
    daily = ts[ts["product"] == "chirps"]
    base = alt.Chart(daily).encode(x=alt.X("date:T", title=None))
    bars = base.mark_bar(color="#9ec5f4", cornerRadiusTopLeft=3, cornerRadiusTopRight=3, size=14).encode(
        y=alt.Y("mean_mm:Q", title="mm"),
        tooltip=[alt.Tooltip("date:T"), alt.Tooltip("mean_mm:Q", title="daily mean (mm)", format=".1f")])
    long = ex.melt(id_vars=["date", "alert_level"], value_vars=["accum_mean_mm", "p95_mean_mm"],
                   var_name="series", value_name="mm")
    long["series"] = long.series.map({"accum_mean_mm": "3-day total", "p95_mean_mm": "P95 threshold"})
    lines = alt.Chart(long).mark_line(strokeWidth=2).encode(
        x="date:T", y="mm:Q",
        color=alt.Color("series:N", scale=alt.Scale(domain=["3-day total", "P95 threshold"],
                                                    range=["#256abf", "#898781"]),
                        legend=alt.Legend(orient="top", title=None, symbolType="stroke")),
        strokeDash=alt.StrokeDash("series:N", scale=alt.Scale(domain=["3-day total", "P95 threshold"],
                                                              range=[[1, 0], [5, 4]]), legend=None),
        tooltip=["date:T", "series:N", alt.Tooltip("mm:Q", format=".1f"), "alert_level:N"])
    dots = alt.Chart(long[long.series == "3-day total"]).mark_point(filled=True, size=40, color="#256abf").encode(
        x="date:T", y="mm:Q", tooltip=["date:T", alt.Tooltip("mm:Q", title="3-day total (mm)", format=".1f"),
                                       "alert_level:N"])
    layers_ts = [bars, lines, dots]
    aux = ts[ts["product"] != "chirps"]
    if not aux.empty:
        layers_ts.append(alt.Chart(aux).mark_line(strokeWidth=1.5, color="#eb6834").encode(
            x="date:T", y="mean_mm:Q", detail="product:N",
            tooltip=["date:T", "product:N", alt.Tooltip("mean_mm:Q", format=".1f")]))
    st.altair_chart(alt.layer(*layers_ts).properties(height=260), use_container_width=True)
    note = (f"Bars: daily district mean ({rain_label}). Blue line: 3-day total. "
            "Grey dashed: P95 threshold.")
    if not aux.empty:
        note += f" Orange: {', '.join(aux['product'].unique())} daily mean (cross-check)."
    st.caption(note)

# ------------------------------------------------------------------------------------------
# CAP, ops log, integrity
# ------------------------------------------------------------------------------------------
tab_cap, tab_log, tab_int = st.tabs(["CAP messages", "Operations log", "Integrity checks"])
with tab_cap:
    cm = q("""SELECT to_char(m.sent AT TIME ZONE 'Africa/Kampala', 'YYYY-MM-DD HH24:MI') AS "sent (EAT)",
                     m.msg_type, m.status, d.name AS district, m.end_date AS "window end",
                     m.severity, m.urgency, m.certainty, m.identifier, m.references_ids,
                     to_char(m.expires AT TIME ZONE 'Africa/Kampala', 'YYYY-MM-DD HH24:MI') AS "expires (EAT)",
                     m.xml::text AS xml
                FROM ews.cap_message m LEFT JOIN ref.district d USING (district_id)
               ORDER BY m.sent DESC, m.created_at DESC""")
    if cm.empty:
        st.info("No CAP messages yet: `python -m ews alert`.")
    else:
        st.dataframe(cm.drop(columns=["xml"]), hide_index=True, use_container_width=True)
        ident = st.selectbox("Message", cm.identifier.tolist())
        xml = cm.loc[cm.identifier == ident, "xml"].iloc[0]
        st.download_button("Download CAP XML", xml.encode(), file_name=f"{ident}.xml", mime="application/xml")
        st.code(xml, language="xml")
with tab_log:
    cats = q("SELECT DISTINCT category FROM ops.operations_log ORDER BY 1").category.tolist()
    pick = st.multiselect("Categories", cats, default=cats)
    log = q("""SELECT to_char(logged_at AT TIME ZONE 'Africa/Kampala', 'YYYY-MM-DD HH24:MI:SS') AS "logged (EAT)",
                      category, severity, component, message, operator, run_id
                 FROM ops.operations_log WHERE category = ANY(:c) ORDER BY log_id DESC LIMIT 300""", c=pick)
    st.dataframe(log, hide_index=True, use_container_width=True)
with tab_int:
    if st.button("Run ops.integrity_checks()"):
        q.clear()
    ic = q("SELECT * FROM ops.integrity_checks()")
    ic.insert(0, "", ic.status.map({"PASS": "✓", "WARN": "!", "FAIL": "✗"}))
    st.dataframe(ic, hide_index=True, use_container_width=True, height=35 * (len(ic) + 1) + 3)
