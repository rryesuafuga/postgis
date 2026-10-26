"""Orchestration: analysis runs, CAP issuance, integrity checks, status and ops reports.

Every function records what it did in ops.pipeline_run / ops.operations_log.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from . import cap, config
from .db import Run, connect, oplog

LEVEL_ICON = {"warning": "[WARNING]", "watch": "[WATCH]", "none": ""}


# --------------------------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------------------------
def loaded_range(table: str = "rain.chirps_daily") -> tuple[date, date]:
    with connect() as conn:
        lo, hi = conn.execute(text(f"SELECT min(obs_date), max(obs_date) FROM {table}")).one()
    if lo is None:
        raise RuntimeError(f"{table} is empty: load rasters first")
    return lo, hi


def compute(start: date | None = None, end: date | None = None, accum_days: int = 3) -> dict:
    """District stats for the loaded days and exceedance for windows ending start..end."""
    lo, hi = loaded_range()
    start = start or lo + timedelta(days=accum_days - 1)
    end = end or hi
    with Run("compute", start=start, end=end, accum_days=accum_days) as run:
        with connect() as conn:
            n_stats = conn.execute(
                text("SELECT ews.compute_district_stats('rain.chirps_daily'::regclass, 'chirps', :s, :e, :r)"),
                {"s": start - timedelta(days=accum_days - 1), "e": end, "r": run.run_id}).scalar_one()
            n_exc = conn.execute(text("SELECT ews.compute_exceedance(:s, :e, :r, :d)"),
                                 {"s": start, "e": end, "r": run.run_id, "d": accum_days}).scalar_one()
            flagged = conn.execute(text("""
                SELECT d.name, d.pcode, max(l.rank) AS rank,
                       (array_agg(x.alert_level ORDER BY l.rank DESC, x.ratio_to_p95 DESC))[1] AS level,
                       min(x.end_date) AS first, max(x.end_date) AS last,
                       max(x.ratio_to_p95) AS peak_ratio, max(x.accum_max_mm) AS peak_mm,
                       count(*) AS days
                  FROM ews.district_exceedance x
                  JOIN ref.district d USING (district_id)
                  JOIN ews.alert_level l ON l.level = x.alert_level
                 WHERE x.end_date BETWEEN :s AND :e AND x.accum_days = :d AND x.alert_level <> 'none'
                 GROUP BY d.name, d.pcode ORDER BY rank DESC, peak_ratio DESC"""),
                {"s": start, "e": end, "d": accum_days}).mappings().all()
        run.rows = n_stats + n_exc
        run.message = f"{len(flagged)} districts flagged"
    oplog("PROCESS", "analysis", "INFO",
          f"Computed {n_stats} district-day stats and {n_exc} {accum_days}-day exceedance rows "
          f"for {start}..{end}; {len(flagged)} district(s) flagged",
          {"start": start, "end": end, "stats_rows": n_stats, "exceedance_rows": n_exc}, run.run_id)
    for f in flagged:
        oplog("EXCEEDANCE", "analysis", "WARN",
              f"{f['name']} ({f['pcode']}): {f['level']} on {f['days']} day(s) {f['first']}..{f['last']}, "
              f"peak {f['peak_ratio']:.2f}x P95, wettest pixel {f['peak_mm']:.0f} mm",
              dict(f), run.run_id)
    return {"run_id": run.run_id, "stats_rows": n_stats, "exceedance_rows": n_exc,
            "flagged": [dict(f) for f in flagged]}


def compute_aux(name: str) -> int:
    table = f"rain.aux_{name}"
    lo, hi = loaded_range(table)
    with Run("compute-aux", name=name, start=lo, end=hi) as run:
        with connect() as conn:
            n = conn.execute(text("SELECT ews.compute_district_stats(CAST(:t AS regclass), :p, :s, :e, :r)"),
                             {"t": table, "p": name, "s": lo, "e": hi, "r": run.run_id}).scalar_one()
        run.rows = n
    oplog("PROCESS", f"aux:{name}", "INFO", f"Computed {n} district-day stats for {name} ({lo}..{hi})",
          run_id=run.run_id)
    return n


# --------------------------------------------------------------------------------------------
# CAP issuance with lifecycle checks
# --------------------------------------------------------------------------------------------
def _pick_target(conn, pcode: str | None, end_date: date | None) -> tuple[str, date]:
    q = """SELECT d.pcode, x.end_date FROM ews.district_exceedance x
             JOIN ref.district d USING (district_id) JOIN ews.alert_level l ON l.level = x.alert_level
            WHERE x.accum_days = 3 AND (CAST(:p AS text) IS NULL OR d.pcode = :p)
              AND (CAST(:e AS date) IS NULL OR x.end_date = :e)
            ORDER BY l.rank DESC, x.ratio_to_p95 DESC NULLS LAST, x.end_date DESC
            LIMIT 1"""
    row = conn.execute(text(q), {"p": pcode, "e": end_date}).first()
    if row is None:
        raise RuntimeError("no exceedance results match: run compute first, or check --district/--date")
    return row.pcode, row.end_date


def _chain(conn, district_id: int) -> list[dict]:
    """Messages of the district's current incident: the latest Alert and everything after it."""
    rows = conn.execute(text("""
        SELECT identifier, sender, sent, msg_type, expires, xml::text AS xml
          FROM ews.cap_message WHERE district_id = :d AND status <> 'Draft'   -- drafts are templates
         ORDER BY sent, created_at"""),
        {"d": district_id}).mappings().all()
    last_alert = max((i for i, r in enumerate(rows) if r["msg_type"] == "Alert"), default=None)
    return [dict(r) for r in rows[last_alert:]] if last_alert is not None else []


def issue_alert(pcode: str | None = None, end_date: date | None = None, msg_type: str = "Alert",
                status: str = "Exercise", approved_by: str | None = None, force: bool = False,
                expires_hours: int = 48) -> Path:
    with connect() as conn:
        pcode, end_date = _pick_target(conn, pcode, end_date)
        d = conn.execute(text("""SELECT district_id, pcode, name, region, code_scheme, source,
                                        ST_AsGeoJSON(geom) AS geojson
                                   FROM ref.district WHERE pcode = :p"""), {"p": pcode}).mappings().one()
        x = dict(conn.execute(text("""SELECT * FROM ews.district_exceedance
                                       WHERE district_id = :d AND end_date = :e AND accum_days = 3"""),
                              {"d": d["district_id"], "e": end_date}).mappings().one())
        x["rule_descriptions"] = conn.execute(text(
            "SELECT array_agg(description ORDER BY rule_code) FROM ews.trigger_rule WHERE rule_code = ANY(:r)"),
            {"r": x["triggered_rules"]}).scalar_one() or []
        lvl_name = x["alert_level"] if x["alert_level"] != "none" else "watch"
        level = dict(conn.execute(text("SELECT * FROM ews.alert_level WHERE level = :l"),
                                  {"l": lvl_name}).mappings().one())
        sources = conn.execute(text("""SELECT string_agg(DISTINCT product_source, ', ')
                                         FROM rain.chirps_daily WHERE obs_date BETWEEN :e - 2 AND :e"""),
                               {"e": end_date}).scalar_one() or "unknown"
        chain = _chain(conn, d["district_id"])
        run_id = conn.execute(text("SELECT max(run_id) FROM ews.district_exceedance WHERE district_id = :d"),
                              {"d": d["district_id"]}).scalar_one()

    synthetic = "SYNTHETIC" in sources.upper() or "SYNTHETIC" in d["source"].upper()
    if status == "Actual" and synthetic:
        raise RuntimeError("status Actual refused: the inputs are SYNTHETIC test data")
    if status == "Actual" and not approved_by:
        raise RuntimeError("status Actual requires --approved-by (the designated national approving authority)")
    if synthetic and status not in ("Test", "Draft"):
        print(f"  synthetic inputs: status {status} -> Test")
        status = "Test"

    now = datetime.now(cap.EAT)
    active = bool(chain) and chain[-1]["msg_type"] != "Cancel" and chain[-1]["expires"] > now
    if msg_type == "Alert" and x["alert_level"] == "none" and not force:
        raise RuntimeError(f"{d['name']} is not flagged on {end_date}; use --force to issue anyway")
    if msg_type == "Alert" and active and not force:
        raise RuntimeError(f"{d['name']} already has an active alert ({chain[-1]['identifier']}); "
                           "issue --msg-type Update or Cancel instead")
    if msg_type in ("Update", "Cancel") and (not chain or chain[-1]["msg_type"] == "Cancel"):
        raise RuntimeError(f"no open incident for {d['name']} to {msg_type.lower()}")

    references = [(r["sender"], r["identifier"], cap.cap_datetime(r["sent"].astimezone(cap.EAT)))
                  for r in chain] if msg_type in ("Update", "Cancel") else []
    # Every message of an incident carries the incident id set by its opening Alert.
    incident = None
    if chain and msg_type != "Alert":
        incident = cap.summary(chain[0]["xml"].encode()).get("incidents")
    incident = incident or f"{config.CAP['identifier_prefix']}-{pcode}-{end_date:%Y%m%d}".replace(".", "-")

    # Identifiers must be unique per sender: suffix a counter if one was issued this second.
    base = f"{config.CAP['identifier_prefix']}.{pcode}.{now:%Y%m%dT%H%M%S}"
    with connect() as conn:
        taken = set(conn.execute(text("SELECT identifier FROM ews.cap_message WHERE identifier LIKE :b || '%'"),
                                 {"b": base}).scalars())
    identifier = next(i for i in (base if n == 0 else f"{base}-{n}" for n in range(1000)) if i not in taken)
    a = cap.AlertInput(
        identifier=identifier, district=d, exceedance=x, level=level,
        polygons=cap.cap_polygons(json.loads(d["geojson"])), data_source=sources, status=status,
        msg_type=msg_type, references=references, incident=incident, sent=now,
        expires_hours=expires_hours, approved_by=approved_by, run_id=run_id)
    xml = cap.build(a)
    errors = cap.validate(xml)

    out_dir = config.out_dir() / "cap"
    if errors:
        rejected = out_dir / "rejected" / f"{identifier}.xml"
        rejected.parent.mkdir(parents=True, exist_ok=True)
        rejected.write_bytes(xml)
        oplog("FAULT", "cap", "ERROR", f"CAP {identifier} failed validation; not issued",
              {"errors": errors, "file": str(rejected)})
        raise RuntimeError("CAP validation failed:\n  " + "\n  ".join(errors))

    path = out_dir / f"{identifier}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(xml)
    s = cap.summary(xml)
    with connect() as conn:
        conn.execute(text("""
            INSERT INTO ews.cap_message (identifier, sender, sent, status, msg_type, scope, references_ids,
                district_id, end_date, alert_level, event, urgency, severity, certainty, expires, headline,
                approved_by, validated, xml, file_path)
            VALUES (:identifier, :sender, CAST(:sent AS timestamptz), :status, :msgType, 'Public', :references,
                :district_id, :end_date, :level, :event, :urgency, :severity, :certainty,
                CAST(:expires AS timestamptz), :headline, :approved_by, true, CAST(:xml AS xml), :file)"""),
            {**s, "district_id": d["district_id"], "end_date": end_date, "level": x["alert_level"],
             "approved_by": approved_by, "xml": xml.decode(), "file": str(path)})
    return path


# --------------------------------------------------------------------------------------------
# Integrity, status, reports
# --------------------------------------------------------------------------------------------
def integrity(expected: int | None = None) -> int:
    if expected is None:
        env_file = config.data_dir() / "boundaries" / "district_source.env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("EWS_EXPECTED_DISTRICTS="):
                    v = line.split("=", 1)[1].strip("'\"")
                    expected = int(v) if v.isdigit() else None
    with connect() as conn:
        rows = conn.execute(text("SELECT * FROM ops.integrity_checks(:n)"), {"n": expected}).mappings().all()
    width = max(len(r["check_name"]) for r in rows)
    for r in rows:
        print(f"  {r['status']:<4}  {r['check_name']:<{width}}  {r['detail']}")
    counts = {s: sum(r["status"] == s for r in rows) for s in ("PASS", "WARN", "FAIL")}
    sev = "ERROR" if counts["FAIL"] else "WARN" if counts["WARN"] else "INFO"
    oplog("INTEGRITY", "db", sev, f"Integrity checks: {counts['PASS']} pass, {counts['WARN']} warn, "
          f"{counts['FAIL']} fail", {"results": [dict(r) for r in rows if r["status"] != "PASS"]})
    return 1 if counts["FAIL"] else 0


def status() -> int:
    """One-screen health summary for the duty officer (also logged as a STATUS entry)."""
    with connect() as conn:
        v = conn.execute(text("SELECT postgis_lib_version(), version()")).one()
        info = conn.execute(text("""
            SELECT (SELECT count(*) FROM ref.district) AS districts,
                   (SELECT max(obs_date) FROM rain.chirps_daily) AS latest_data,
                   (SELECT max(end_date) FROM ews.district_exceedance) AS latest_result,
                   (SELECT count(*) FROM ews.v_flagged
                     WHERE end_date = (SELECT max(end_date) FROM ews.district_exceedance)) AS flagged_latest,
                   (SELECT max(finished_at) FROM ops.pipeline_run WHERE status = 'success') AS last_success,
                   (SELECT count(*) FROM ops.pipeline_run WHERE status = 'failed'
                       AND started_at > now() - interval '24 hours') AS failed_24h,
                   (SELECT max(logged_at) FROM ops.operations_log WHERE category = 'BACKUP'
                       AND details ->> 'verified' = 'true') AS last_backup,
                   (SELECT count(*) FROM ops.operations_log WHERE category = 'FAULT'
                       AND logged_at > now() - interval '24 hours') AS faults_24h,
                   (SELECT count(*) FROM ews.v_cap_lifecycle WHERE is_latest AND msg_type <> 'Cancel'
                       AND expires > now()) AS active_alerts""")).mappings().one()
    print(f"  PostGIS {v[0]} | {v[1].split(',')[0]}")
    for k, val in info.items():
        print(f"  {k:<15} {val}")
    if info["latest_data"]:
        lag = (date.today() - info["latest_data"]).days
        print(f"  data latency    {lag} day(s) behind today"
              + ("  (historical demo window)" if lag > 60 else ""))
    oplog("STATUS", "system", "WARN" if info["failed_24h"] or info["faults_24h"] else "INFO",
          "Status check", dict(info))
    return 0


def ops_report(month: str | None = None) -> Path:
    """Monthly operations report (Markdown) from ops.operations_log and ews.cap_message."""
    first = date.fromisoformat((month or f"{date.today():%Y-%m}") + "-01")
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    p = {"a": first, "b": nxt}
    with connect() as conn:
        runs = conn.execute(text("""SELECT status, count(*) FROM ops.pipeline_run
                                     WHERE started_at >= :a AND started_at < :b GROUP BY 1 ORDER BY 1"""), p).all()
        cats = conn.execute(text("""SELECT category, severity, count(*) FROM ops.operations_log
                                     WHERE logged_at >= :a AND logged_at < :b GROUP BY 1, 2 ORDER BY 1, 2"""), p).all()
        alerts = conn.execute(text("""SELECT m.sent, m.msg_type, m.status, d.name, m.severity, m.identifier
                                        FROM ews.cap_message m LEFT JOIN ref.district d USING (district_id)
                                       WHERE m.sent >= :a AND m.sent < :b ORDER BY m.sent"""), p).all()
        faults = conn.execute(text("""SELECT logged_at, category, component, message FROM ops.operations_log
                                       WHERE category IN ('FAULT', 'ACTION', 'ESCALATION')
                                         AND logged_at >= :a AND logged_at < :b ORDER BY logged_at"""), p).all()
        backups = conn.execute(text("""SELECT logged_at, severity, message FROM ops.operations_log
                                        WHERE category = 'BACKUP' AND logged_at >= :a AND logged_at < :b
                                        ORDER BY logged_at"""), p).all()
    lines = [f"# Operations report {first:%B %Y}", "",
             f"Generated {datetime.now(cap.EAT):%Y-%m-%d %H:%M} EAT from ops.operations_log.", "",
             "## Pipeline runs", "", "| status | runs |", "|---|---|"]
    lines += [f"| {s} | {n} |" for s, n in runs] or ["| - | 0 |"]
    lines += ["", "## Log entries by category", "", "| category | severity | entries |", "|---|---|---|"]
    lines += [f"| {c} | {s} | {n} |" for c, s, n in cats]
    lines += ["", "## CAP messages", "", "| sent (EAT) | type | status | area | severity | identifier |",
              "|---|---|---|---|---|---|"]
    lines += [f"| {t.astimezone(cap.EAT):%Y-%m-%d %H:%M} | {m} | {s} | {n} | {sv} | `{i}` |"
              for t, m, s, n, sv, i in alerts] or ["| - | | | | | |"]
    lines += ["", "## Faults, actions and escalations", ""]
    lines += [f"- {t.astimezone(cap.EAT):%Y-%m-%d %H:%M} **{c}** {comp}: {msg}" for t, c, comp, msg in faults] \
        or ["- none recorded"]
    lines += ["", "## Backups", ""]
    lines += [f"- {t.astimezone(cap.EAT):%Y-%m-%d %H:%M} {s}: {m}" for t, s, m in backups] or ["- none recorded"]
    path = config.out_dir() / "reports" / f"ops_report_{first:%Y-%m}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path
