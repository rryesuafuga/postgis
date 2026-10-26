"""Downloaders for the demo's open datasets.

CHIRPS v2.0 (UCSB Climate Hazards Center), public domain
  * daily Africa GeoTIFFs   data.chc.ucsb.edu/products/CHIRPS-2.0/africa_daily/tifs/p05/<yyyy>/
  * the same product as a subsettable NetCDF from NOAA ERDDAP (dataset chirps20GlobalDailyP05),
    which keeps the 1991-2020 climatology download to ~3 MB per year instead of ~1,500 Africa files.
District boundaries
  * OCHA COD-AB Uganda on HDX (dataset cod-ab-uga, admin 2 = district), via the HDX CKAN API
  * GADM 4.1 GeoPackage (the district layer is picked by feature count)
Auxiliary layers
  * GPM IMERG daily NetCDF (Earthdata login required; download it yourself) or an ICPAC seasonal
    forecast GeoTIFF/NetCDF from the ICPAC data portals, converted by `prep_aux`.

Every raster is cropped/regridded to the analysis grid (ews.grid) before it reaches PostGIS.
"""

from __future__ import annotations

import gzip
import shlex
import shutil
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests
import xarray as xr

from . import grid

USER_AGENT = "uganda-ews-demo/0.1"
CHC_AFRICA_DAILY = ("https://data.chc.ucsb.edu/products/CHIRPS-2.0/africa_daily/tifs/p05/"
                    "{y}/chirps-v2.0.{y}.{m:02d}.{d:02d}.tif.gz")
CHC_GLOBAL_DAILY = ("https://data.chc.ucsb.edu/products/CHIRPS-2.0/global_daily/tifs/p05/"
                    "{y}/chirps-v2.0.{y}.{m:02d}.{d:02d}.tif.gz")
ERDDAP_CHIRPS = "https://coastwatch.pfeg.noaa.gov/erddap/griddap/chirps20GlobalDailyP05.nc"
HDX_PACKAGE = "https://data.humdata.org/api/3/action/package_show?id={id}"
GADM_GPKG = "https://geodata.ucdavis.edu/gadm/gadm4.1/gpkg/gadm41_{iso}.gpkg"


class FetchError(RuntimeError):
    pass


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def download(url: str, dest: Path, session: requests.Session | None = None,
             retries: int = 4, timeout: int = 120) -> Path:
    """Stream `url` to `dest` atomically, retrying transient failures with backoff."""
    session = session or _session()
    dest.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with session.get(url, stream=True, timeout=timeout) as r:
                if r.status_code == 404:
                    raise FetchError(f"404 Not Found: {url}")
                r.raise_for_status()
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                tmp.replace(dest)
                return dest
        except FetchError:
            raise
        except Exception as e:  # network error, 5xx
            last = e
            time.sleep(2 ** (attempt + 1))
    raise FetchError(f"download failed after {retries} attempts: {url}: {last}")


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# --------------------------------------------------------------------------------------------
# CHIRPS daily
# --------------------------------------------------------------------------------------------
def chirps_filename(day: date) -> str:
    return f"chirps-v2.0.{day:%Y.%m.%d}.tif"


def _crop_geotiff_to_grid(src_tif: Path) -> np.ndarray:
    import rioxarray

    da = rioxarray.open_rasterio(src_tif, masked=True).squeeze("band", drop=True)
    return grid.to_grid(da).values


def fetch_chirps_day_chc(day: date, out_dir: Path, raw_dir: Path | None = None,
                         force: bool = False, session: requests.Session | None = None) -> Path:
    """Download one CHC daily GeoTIFF (Africa, falling back to global) and crop it to the grid."""
    out = out_dir / chirps_filename(day)
    if out.exists() and not force:
        return out
    with tempfile.TemporaryDirectory() as tmp:
        gz = Path(tmp) / "day.tif.gz"
        url = None
        for tpl in (CHC_AFRICA_DAILY, CHC_GLOBAL_DAILY):
            url = tpl.format(y=day.year, m=day.month, d=day.day)
            try:
                download(url, gz, session)
                break
            except FetchError:
                url = None
        if url is None:
            raise FetchError(f"CHIRPS {day} not found at CHC (final data for a month appear around the "
                             "third week of the next month)")
        tif = Path(tmp) / "day.tif"
        with gzip.open(gz, "rb") as fi, open(tif, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(gz, raw_dir / Path(url).name)
        arr = _crop_geotiff_to_grid(tif)
    grid.write_geotiff(out, arr, tags={"product": "CHIRPS v2.0", "units": "mm/day",
                                       "date": day.isoformat(), "source_url": url})
    return out


def erddap_chirps(start: date, end: date, session: requests.Session | None = None) -> xr.DataArray:
    """Daily CHIRPS v2.0 over the grid bounds from NOAA ERDDAP, as (time, y, x) on the grid."""
    session = session or _session()
    lat0, lat1 = grid.SOUTH + grid.RES / 2, grid.NORTH - grid.RES / 2
    lon0, lon1 = grid.WEST + grid.RES / 2, grid.EAST - grid.RES / 2
    t = f"[({start:%Y-%m-%d}T00:00:00Z):1:({end:%Y-%m-%d}T00:00:00Z)]"
    last = None
    # ERDDAP wants the axis order of the dataset; try ascending latitude first, then descending.
    for la, lb in ((lat0, lat1), (lat1, lat0)):
        query = f"precip{t}[({la:.3f}):1:({lb:.3f})][({lon0:.3f}):1:({lon1:.3f})]"
        url = f"{ERDDAP_CHIRPS}?{quote(query, safe='():,.-')}"
        with tempfile.TemporaryDirectory() as tmp:
            nc = Path(tmp) / "subset.nc"
            try:
                download(url, nc, session, timeout=600)
            except FetchError as e:
                last = e
                continue
            with xr.open_dataset(nc) as ds:
                da = ds["precip"].load()
        da = grid.to_grid(da)
        da["time"] = pd.to_datetime(da["time"].values).normalize()
        return da
    raise FetchError(f"ERDDAP request failed: {last}")


def fetch_chirps(start: date, end: date, out_dir: Path, source: str = "chc", force: bool = False,
                 raw_dir: Path | None = None, workers: int = 6) -> list[Path]:
    """Daily CHIRPS GeoTIFFs on the grid for start..end (inclusive)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    days = list(daterange(start, end))
    todo = [d for d in days if force or not (out_dir / chirps_filename(d)).exists()]
    if todo and source == "erddap":
        da = erddap_chirps(min(todo), max(todo))
        for t in da["time"].values:
            day = pd.Timestamp(t).date()
            if day in todo:
                grid.write_geotiff(out_dir / chirps_filename(day), da.sel(time=t).values,
                                   tags={"product": "CHIRPS v2.0", "units": "mm/day",
                                         "date": day.isoformat(), "source_url": ERDDAP_CHIRPS})
    elif todo:
        session = _session()
        failed = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(fetch_chirps_day_chc, d, out_dir, raw_dir, force, session): d for d in todo}
            for fut in as_completed(futs):
                try:
                    print(f"  {fut.result().name}")
                except FetchError as e:
                    failed.append(futs[fut])
                    print(f"  ! {e}")
        if failed:
            raise FetchError(f"{len(failed)} day(s) failed from CHC: {sorted(failed)}; "
                             "retry, or use --source erddap")
    return [out_dir / chirps_filename(d) for d in days]


# --------------------------------------------------------------------------------------------
# Climatology inputs (1991-2020, seasonal window only)
# --------------------------------------------------------------------------------------------
def same_day_in_year(d: date, year: int) -> date:
    try:
        return d.replace(year=year)
    except ValueError:          # 29 February
        return d.replace(year=year, day=28)


def clim_window(event_start: date, event_end: date, year: int, half_window: int,
                accum_days: int = 3) -> tuple[date, date]:
    """Days needed in `year` for N-day totals ending within the event window +/- half_window."""
    s = same_day_in_year(event_start, year) - timedelta(days=half_window + accum_days - 1)
    e = s + (event_end - event_start) + timedelta(days=2 * half_window + accum_days - 1)
    return s, e


def fetch_climatology_inputs(event_start: date, event_end: date, years: tuple[int, int],
                             half_window: int, out_dir: Path, source: str = "erddap",
                             force: bool = False) -> list[Path]:
    """One NetCDF per baseline year holding the seasonal window of daily CHIRPS on the grid."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for year in range(years[0], years[1] + 1):
        s, e = clim_window(event_start, event_end, year, half_window)
        path = out_dir / f"chirps_{year}_{s:%m%d}-{e:%m%d}.nc"
        paths.append(path)
        if path.exists() and not force:
            continue
        print(f"  {year}: {s} .. {e} via {source}")
        if source == "erddap":
            da = erddap_chirps(s, e)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                files = fetch_chirps(s, e, Path(tmp), source="chc")
                da = xr.concat([xr.DataArray(grid.read_geotiff(f), coords={"y": grid.LATS, "x": grid.LONS},
                                             dims=("y", "x")) for f in files],
                               dim=pd.Index(pd.to_datetime([d for d in daterange(s, e)]), name="time"))
        da.name = "precip"
        da.attrs.update(units="mm/day", source="CHIRPS v2.0")
        da.to_dataset().to_netcdf(path, encoding={"precip": {"zlib": True, "complevel": 4}})
    return paths


# --------------------------------------------------------------------------------------------
# District boundaries
# --------------------------------------------------------------------------------------------
def write_source_env(boundary_dir: Path, src: str, layer: str | None, label: str, scheme: str,
                     expected: int | None, **fields: str) -> Path:
    """Describe the boundary source for scripts/10_load_districts.sh (sourced by bash)."""
    values = {"EWS_DISTRICT_SRC": src, "EWS_DISTRICT_LAYER": layer or "",
              "EWS_DISTRICT_SOURCE_LABEL": label, "EWS_DISTRICT_CODE_SCHEME": scheme,
              "EWS_EXPECTED_DISTRICTS": str(expected or "")}
    values.update({f"EWS_DISTRICT_{k.upper()}_FIELD": v for k, v in fields.items() if v})
    path = boundary_dir / "district_source.env"
    path.write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in values.items()))
    return path


def _layers(path: str) -> list[tuple[str, int]]:
    import pyogrio

    out = []
    for name, _geom in pyogrio.list_layers(path):
        out.append((name, int(pyogrio.read_info(path, layer=name)["features"])))
    return out


def fetch_districts_hdx(boundary_dir: Path, dataset: str = "cod-ab-uga") -> Path:
    """OCHA COD-AB from HDX: find a shapefile/GeoPackage/GeoJSON resource and its admin-2 layer."""
    s = _session()
    meta = s.get(HDX_PACKAGE.format(id=dataset), timeout=60)
    meta.raise_for_status()
    pkg = meta.json()["result"]
    resources = pkg["resources"]

    def rank(r):
        name, fmt = (r.get("name") or "").lower(), (r.get("format") or "").lower()
        score = 0
        if "shp" in fmt or name.endswith("shp.zip") or "shapefile" in fmt:
            score = 3
        elif "geopackage" in fmt or name.endswith(".gpkg") or "gpkg" in name:
            score = 2
        elif "geojson" in fmt or "geojson" in name:
            score = 1
        return score

    best = max(resources, key=rank)
    if rank(best) == 0:
        raise FetchError(f"no vector resource found in HDX dataset {dataset}: "
                         f"{[r.get('name') for r in resources]}")
    url = best.get("download_url") or best["url"]
    dest = boundary_dir / "hdx" / Path(url.split("?")[0]).name
    print(f"  HDX {dataset}: {best.get('name')} ({best.get('format')}) -> {dest}")
    download(url, dest, s)

    if dest.suffix == ".zip":
        with zipfile.ZipFile(dest) as z:
            members = z.namelist()
        shp = [m for m in members if m.lower().endswith(".shp") and "adm2" in m.lower()]
        if shp:
            src, layer = f"/vsizip/{dest}/{shp[0]}", None
        else:
            extract = dest.with_suffix("")
            with zipfile.ZipFile(dest) as z:
                z.extractall(extract)
            cands = [p for p in extract.rglob("*") if p.suffix.lower() in (".gpkg", ".geojson", ".json", ".gdb")]
            if not cands:
                raise FetchError(f"no admin-2 layer found in {dest}: {members[:20]}")
            src, layer = str(cands[0]), None
    else:
        src, layer = str(dest), None

    if layer is None:
        layers = _layers(src)
        adm2 = [n for n, _ in layers if "adm2" in n.lower()]
        layer = adm2[0] if adm2 else (layers[0][0] if len(layers) == 1 else None)
        if layer is None:
            raise FetchError(f"cannot pick the admin-2 layer in {src}: {layers}")
    n = dict(_layers(src)).get(layer)
    label = f"OCHA COD-AB Uganda (HDX {dataset}), admin 2"
    print(f"  layer {layer}: {n} features")
    return write_source_env(boundary_dir, src, layer, label, "OCHA-PCODE", 135)


def fetch_districts_gadm(boundary_dir: Path, iso: str = "UGA") -> Path:
    """GADM 4.1: choose the layer whose feature count looks like Uganda's districts (100-200)."""
    url = GADM_GPKG.format(iso=iso)
    dest = boundary_dir / "gadm" / Path(url).name
    print(f"  GADM 4.1 -> {dest}")
    download(url, dest)
    layers = _layers(str(dest))
    print("  layers: " + ", ".join(f"{n} ({c})" for n, c in layers))
    district = [(n, c) for n, c in layers if 100 <= c <= 200]
    if not district:
        raise FetchError(f"no layer with 100-200 features in {dest}; set the layer manually")
    layer, count = district[0]
    level = layer.rsplit("_", 1)[-1]
    return write_source_env(boundary_dir, str(dest), layer, f"GADM 4.1 {iso} level {level}", "GADM-GID",
                            count, name=f"name_{level}", code=f"gid_{level}",
                            region=f"name_{int(level) - 1}" if level.isdigit() and int(level) > 1 else "")


# --------------------------------------------------------------------------------------------
# Auxiliary layers: GPM IMERG daily NetCDF, ICPAC seasonal forecast GeoTIFF/NetCDF
# --------------------------------------------------------------------------------------------
def prep_aux(path: Path, name: str, out_root: Path, var: str | None = None,
             on_date: date | None = None, units: str = "") -> list[Path]:
    """Regrid an auxiliary product to the analysis grid as <name>.YYYY.MM.DD.tif files."""
    out_dir = out_root / name
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    if path.suffix.lower() in (".tif", ".tiff"):
        import rioxarray

        da = rioxarray.open_rasterio(path, masked=True)
        da = da.isel(band=0, drop=True) if "band" in da.dims else da
        if on_date is None:
            raise ValueError("--date is required for GeoTIFF input (valid/issue date)")
        stack = [(on_date, grid.to_grid(da).values)]
    else:
        ds = xr.open_dataset(path)
        if var is None:   # IMERG V07: precipitation; V06: precipitationCal
            var = next((v for v in ("precipitation", "precipitationCal", "precip", "prec", "prob")
                        if v in ds.data_vars), None) or list(ds.data_vars)[0]
        da = ds[var]
        units = units or da.attrs.get("units", "")
        da = grid.to_grid(da)
        if "time" in da.dims:
            stack = [(pd.Timestamp(t).date(), da.sel(time=t).values) for t in da["time"].values]
        else:
            if on_date is None:
                raise ValueError("--date is required: input has no time dimension")
            stack = [(on_date, da.values)]
    for d, arr in stack:
        out = out_dir / f"{name}.{d:%Y.%m.%d}.tif"
        grid.write_geotiff(out, arr, tags={"product": name, "units": units, "date": d.isoformat(),
                                           "source_file": path.name})
        written.append(out)
    return written
