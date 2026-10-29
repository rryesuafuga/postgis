"""Command line: `python -m ews <command> --help`."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from . import config


def _d(s: str) -> date:
    return date.fromisoformat(s)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m ews", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    ev_start, ev_end = config.event_window()

    s = sub.add_parser("synthetic", help="generate the offline SYNTHETIC test fixture")
    s.add_argument("--seed", type=int, default=20220730)

    s = sub.add_parser("fetch-districts", help="download district boundaries (geoBoundaries, HDX COD-AB or GADM)")
    s.add_argument("--source", choices=["geoboundaries", "hdx", "gadm"],
                   default=config.env("EWS_DISTRICT_SOURCE", "geoboundaries"))

    s = sub.add_parser("fetch-chirps", help="download daily CHIRPS for the event window (+2 lead days)")
    s.add_argument("--start", type=_d, default=ev_start - timedelta(days=2))
    s.add_argument("--end", type=_d, default=ev_end)
    s.add_argument("--source", choices=["chc", "erddap"], default="chc")
    s.add_argument("--force", action="store_true")
    s.add_argument("--keep-raw", action="store_true", help="keep the original Africa .tif.gz files")

    s = sub.add_parser("fetch-climatology", help="download baseline-year inputs for the seasonal window")
    s.add_argument("--source", choices=["erddap", "chc"], default="erddap")
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("climatology", help="build the P95 3-day climatology GeoTIFF with xarray")
    s.add_argument("--q", type=float, default=0.95)
    s.add_argument("--accum-days", type=int, default=3)

    s = sub.add_parser("prep-aux", help="regrid an IMERG / ICPAC layer for loading")
    s.add_argument("--input", type=Path, required=True)
    s.add_argument("--name", required=True, help="layer name, e.g. imerg or icpac_ond2026")
    s.add_argument("--var")
    s.add_argument("--date", type=_d)
    s.add_argument("--units", default="")

    s = sub.add_parser("compute", help="district stats + 3-day exceedance (SQL functions)")
    s.add_argument("--start", type=_d)
    s.add_argument("--end", type=_d)
    s.add_argument("--accum-days", type=int, default=3)

    s = sub.add_parser("compute-aux", help="district stats for an auxiliary layer")
    s.add_argument("--name", required=True)

    s = sub.add_parser("alert", help="export a CAP 1.2 message for a flagged district")
    s.add_argument("--district", help="district name or code; default = highest-ranked flagged district")
    s.add_argument("--date", type=_d, help="3-day window end date; default = latest flagged")
    s.add_argument("--msg-type", choices=["Alert", "Update", "Cancel"], default="Alert")
    s.add_argument("--status", choices=["Exercise", "Test", "Draft", "Actual"], default="Exercise")
    s.add_argument("--approved-by")
    s.add_argument("--expires-hours", type=int, default=48)
    s.add_argument("--force", action="store_true")
    s.add_argument("--update-if-active", action="store_true",
                   help="issue an Update instead of a second Alert when the district already has one")

    s = sub.add_parser("validate-cap", help="validate CAP XML files")
    s.add_argument("files", nargs="+", type=Path)

    s = sub.add_parser("integrity", help="run geodatabase integrity checks")
    s.add_argument("--expected", type=int)

    sub.add_parser("status", help="health summary for the duty officer")

    s = sub.add_parser("log", help="record an operations-log entry (fault, action, escalation...)")
    s.add_argument("category")
    s.add_argument("component")
    s.add_argument("severity", choices=["INFO", "WARN", "ERROR"])
    s.add_argument("message")
    s.add_argument("--details", default=None, help="JSON object")

    s = sub.add_parser("site", help="export the static demo page (docs/index.html, GitHub Pages)")
    s.add_argument("--out", type=Path, default=None)

    s = sub.add_parser("report", help="monthly operations report (Markdown)")
    s.add_argument("--month", help="YYYY-MM (default: current month)")

    a = p.parse_args(argv)
    data = config.data_dir()

    if a.cmd == "synthetic":
        from .synthetic import make_all

        print(json.dumps(make_all(data, ev_start, ev_end, config.clim_years(), config.clim_half_window(),
                                  a.seed), indent=2))
    elif a.cmd == "fetch-districts":
        from . import fetch

        fn = {"geoboundaries": fetch.fetch_districts_geoboundaries, "hdx": fetch.fetch_districts_hdx,
              "gadm": fetch.fetch_districts_gadm}[a.source]
        print(f"wrote {fn(data / 'boundaries')}")
    elif a.cmd == "fetch-chirps":
        from . import fetch

        files = fetch.fetch_chirps(a.start, a.end, data / "chirps" / "daily", a.source, a.force,
                                   raw_dir=data / "raw" / "chirps" if a.keep_raw else None)
        print(f"{len(files)} daily files in {data / 'chirps' / 'daily'}")
    elif a.cmd == "fetch-climatology":
        from . import fetch

        files = fetch.fetch_climatology_inputs(ev_start, ev_end, config.clim_years(),
                                               config.clim_half_window(), data / "clim" / "inputs",
                                               a.source, a.force)
        print(f"{len(files)} yearly inputs in {data / 'clim' / 'inputs'}")
    elif a.cmd == "climatology":
        from .climatology import build_to_geotiff

        inputs = sorted((data / "clim" / "inputs").glob("*.nc"))
        out = build_to_geotiff(inputs, data / "clim", ev_start, ev_end, config.clim_years(),
                               config.clim_half_window(), a.accum_days, a.q)
        print(f"wrote {out}")
    elif a.cmd == "prep-aux":
        from .fetch import prep_aux

        files = prep_aux(a.input, a.name, data / "aux", a.var, a.date, a.units)
        print(f"{len(files)} file(s) in {data / 'aux' / a.name}")
    elif a.cmd == "compute":
        from .pipeline import compute

        r = compute(a.start, a.end, a.accum_days)
        print(f"run {r['run_id']}: {r['stats_rows']} stats rows, {r['exceedance_rows']} exceedance rows")
        for f in r["flagged"]:
            print(f"  {f['level']:<8} {f['name']:<32} {f['first']}..{f['last']}  "
                  f"peak {f['peak_ratio']:.2f}x P95, max {f['peak_mm']:.0f} mm")
    elif a.cmd == "compute-aux":
        from .pipeline import compute_aux

        print(f"{compute_aux(a.name)} rows")
    elif a.cmd == "alert":
        from .pipeline import issue_alert

        path = issue_alert(a.district, a.date, a.msg_type, a.status, a.approved_by, a.force, a.expires_hours,
                           a.update_if_active)
        from .cap import as_json

        print(f"wrote {path}\n{as_json(path.read_bytes())}")
    elif a.cmd == "validate-cap":
        from .cap import XSD_PATH, validate

        print(f"(official XSD {'found' if XSD_PATH.exists() else 'not found: rule checks only'})")
        bad = 0
        for f in a.files:
            errs = validate(f.read_bytes())
            print(f"{'VALID  ' if not errs else 'INVALID'} {f}")
            for e in errs:
                print(f"    - {e}")
            bad += bool(errs)
        return 1 if bad else 0
    elif a.cmd == "integrity":
        from .pipeline import integrity

        return integrity(a.expected)
    elif a.cmd == "status":
        from .pipeline import status

        return status()
    elif a.cmd == "log":
        from .db import oplog

        oplog(a.category.upper(), a.component, a.severity, a.message,
              json.loads(a.details) if a.details else None)
    elif a.cmd == "site":
        from .site import export

        print(f"wrote {export(a.out)}")
    elif a.cmd == "report":
        from .pipeline import ops_report

        print(f"wrote {ops_report(a.month)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
