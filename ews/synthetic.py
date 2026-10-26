"""SYNTHETIC test fixture for running the whole pipeline offline.

Nothing produced here is an observation. It exists so the loaders, SQL, CAP export, integrity
checks, backups and the map can be exercised without network access to CHC/HDX/GADM. Every
artefact is labelled: file names start with `synthetic`, districts carry source "SYNTHETIC",
and the CAP exporter forces status=Test when it sees synthetic inputs.

  * zones:   Voronoi cells around ~50 town locations, clipped to the Natural Earth 1:50m Uganda
             outline. They are NOT district boundaries.
  * rain:    spatially correlated gamma-like daily fields on the CHIRPS 0.05 degree grid, with a
             crude wet/dry pattern; two storms are injected into the event window
             (Mt Elgon around 28-31 July, Rwenzori around 24-25 July).
  * climatology inputs: the same generator, no injected storms, for every baseline year.
  * an IMERG-like 0.1 degree NetCDF stored (time, lon, lat), to exercise the aux converter.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from . import grid
from .fetch import clim_window, daterange, write_source_env

FIXTURES = Path(__file__).parent / "fixtures"

# (name, lon, lat, region): approximate town locations used only as Voronoi seeds.
TOWNS = [
    ("Kampala", 32.58, 0.35, "Central"), ("Entebbe", 32.46, 0.05, "Central"),
    ("Mukono", 32.75, 0.35, "Central"), ("Mityana", 32.04, 0.40, "Central"),
    ("Luwero", 32.50, 0.84, "Central"), ("Nakasongola", 32.46, 1.31, "Central"),
    ("Kayunga", 32.89, 0.70, "Central"), ("Kiboga", 31.77, 0.92, "Central"),
    ("Mubende", 31.39, 0.56, "Central"), ("Masaka", 31.73, -0.33, "Central"),
    ("Rakai", 31.41, -0.71, "Central"), ("Kalangala", 32.23, -0.31, "Central"),
    ("Jinja", 33.20, 0.44, "Eastern"), ("Iganga", 33.48, 0.61, "Eastern"),
    ("Kamuli", 33.12, 0.95, "Eastern"), ("Busia", 34.09, 0.47, "Eastern"),
    ("Tororo", 34.18, 0.69, "Eastern"), ("Pallisa", 33.71, 1.15, "Eastern"),
    ("Mbale", 34.18, 1.08, "Eastern"), ("Bududa", 34.33, 1.01, "Eastern"),
    ("Kapchorwa", 34.45, 1.40, "Eastern"), ("Kumi", 33.94, 1.49, "Eastern"),
    ("Soroti", 33.61, 1.71, "Eastern"), ("Katakwi", 33.97, 1.89, "Eastern"),
    ("Amuria", 33.64, 2.03, "Eastern"), ("Nakapiripirit", 34.72, 1.85, "Northern"),
    ("Moroto", 34.66, 2.53, "Northern"), ("Kotido", 34.13, 2.98, "Northern"),
    ("Kaabong", 34.12, 3.52, "Northern"), ("Kitgum", 32.89, 3.28, "Northern"),
    ("Pader", 33.08, 2.88, "Northern"), ("Gulu", 32.30, 2.77, "Northern"),
    ("Lira", 32.90, 2.25, "Northern"), ("Apac", 32.54, 1.98, "Northern"),
    ("Arua", 30.91, 3.02, "Northern"), ("Nebbi", 31.09, 2.48, "Northern"),
    ("Adjumani", 31.79, 3.38, "Northern"), ("Moyo", 31.72, 3.66, "Northern"),
    ("Masindi", 31.72, 1.68, "Western"), ("Hoima", 31.35, 1.43, "Western"),
    ("Fort Portal", 30.27, 0.67, "Western"), ("Bundibugyo", 30.06, 0.71, "Western"),
    ("Kasese", 30.08, 0.18, "Western"), ("Bushenyi", 30.19, -0.54, "Western"),
    ("Mbarara", 30.65, -0.61, "Western"), ("Ntungamo", 30.26, -0.88, "Western"),
    ("Rukungiri", 29.93, -0.79, "Western"), ("Kabale", 29.99, -1.25, "Western"),
    ("Kisoro", 29.69, -1.28, "Western"), ("Kyenjojo", 30.64, 0.63, "Western"),
]

# Injected storms: (centre lon, lat, sigma deg, {date: peak mm/day})
STORMS = [
    (34.38, 1.10, 0.22, {"07-28": 30.0, "07-29": 55.0, "07-30": 75.0, "07-31": 35.0}),   # Mt Elgon
    (30.05, 0.25, 0.18, {"07-24": 40.0, "07-25": 45.0}),                                  # Rwenzori
]

LON2D, LAT2D = np.meshgrid(grid.LONS, grid.LATS)


def _bump(lon0: float, lat0: float, sigma: float) -> np.ndarray:
    return np.exp(-((LON2D - lon0) ** 2 + (LAT2D - lat0) ** 2) / (2 * sigma ** 2))


def mean_field() -> np.ndarray:
    """Plausible-looking July/August mean daily rainfall pattern (mm/day). Not a climatology."""
    m = (2.2 + 3.0 * _bump(34.45, 1.15, 0.35) + 2.5 * _bump(29.95, 0.35, 0.40)
         + 2.0 * _bump(32.60, 0.00, 0.80) + 1.5 * _bump(31.00, 2.80, 0.80)
         - 1.4 * _bump(34.20, 3.00, 0.90))
    return np.clip(m, 0.4, None).astype("float32")


def _smooth_noise(rng: np.random.Generator, scale_px: float) -> np.ndarray:
    """Gaussian-correlated N(0,1) field via FFT filtering of white noise."""
    h, w = grid.HEIGHT, grid.WIDTH
    noise = rng.standard_normal((h, w))
    ky = np.fft.fftfreq(h)[:, None]
    kx = np.fft.fftfreq(w)[None, :]
    filt = np.exp(-2 * (np.pi * scale_px) ** 2 * (kx ** 2 + ky ** 2))
    f = np.real(np.fft.ifft2(np.fft.fft2(noise) * filt))
    return (f - f.mean()) / f.std()


def daily_field(rng: np.random.Generator, mean: np.ndarray, wet_fraction: float = 0.5) -> np.ndarray:
    z_occ = _smooth_noise(rng, 6.0)
    z_amt = _smooth_noise(rng, 3.0)
    thresh = np.quantile(z_occ, 1 - wet_fraction)
    wet = z_occ > thresh
    amount = mean / wet_fraction * np.exp(0.9 * z_amt - 0.405)        # lognormal, mean 1
    return np.where(wet, amount, 0.0).astype("float32")


def storm_rain(day: date) -> np.ndarray:
    add = np.zeros((grid.HEIGHT, grid.WIDTH), "float32")
    for lon0, lat0, sigma, peaks in STORMS:
        peak = peaks.get(f"{day:%m-%d}")
        if peak:
            add += peak * _bump(lon0, lat0, sigma)
    return add


def make_zones(out_dir: Path) -> Path:
    import geopandas as gpd
    from shapely import MultiPoint, Point, voronoi_polygons
    from shapely.geometry import MultiPolygon

    outline = gpd.read_file(FIXTURES / "uganda_outline_ne50m.geojson").geometry.iloc[0]
    pts = [Point(lon, lat) for _, lon, lat, _ in TOWNS]
    cells = voronoi_polygons(MultiPoint(pts), extend_to=outline.envelope.buffer(1.0))
    rows = []
    for cell in cells.geoms:
        idx = next(i for i, p in enumerate(pts) if cell.contains(p))
        name, _, _, region = TOWNS[idx]
        geom = cell.intersection(outline)
        if geom.is_empty:
            continue
        geom = geom if geom.geom_type == "MultiPolygon" else MultiPolygon([geom])
        rows.append({"pcode": f"SYN{idx + 1:03d}", "name": f"{name} (synthetic zone)",
                     "region": region, "geometry": geom})
    gdf = gpd.GeoDataFrame(rows, crs="EPSG:4326").sort_values("pcode")
    path = out_dir / "synthetic_zones.gpkg"
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(path, layer="zones", driver="GPKG")
    write_source_env(out_dir, str(path), "zones",
                     "SYNTHETIC test zones (Voronoi on town locations; NOT official boundaries)",
                     "SYNTHETIC-ID", len(gdf))
    return path


def make_all(root: Path, event_start: date, event_end: date, years: tuple[int, int],
             half_window: int, seed: int = 20220730) -> dict:
    rng = np.random.default_rng(seed)
    mean = mean_field()
    summary = {"root": str(root), "seed": seed}

    summary["zones"] = str(make_zones(root / "boundaries"))

    daily_dir = root / "chirps" / "daily"
    days = list(daterange(event_start - timedelta(days=2), event_end))
    series = []
    for d in days:
        arr = daily_field(rng, mean) + storm_rain(d)
        series.append(arr)
        grid.write_geotiff(daily_dir / f"synthetic.{d:%Y.%m.%d}.tif", arr,
                           tags={"product": "SYNTHETIC (not CHIRPS)", "units": "mm/day",
                                 "date": d.isoformat()})
    summary["daily_files"] = len(days)

    clim_dir = root / "clim" / "inputs"
    clim_dir.mkdir(parents=True, exist_ok=True)
    for year in range(years[0], years[1] + 1):
        s, e = clim_window(event_start, event_end, year, half_window)
        yr = np.random.default_rng(seed + year)
        cube = np.stack([daily_field(yr, mean) for _ in daterange(s, e)])
        da = xr.DataArray(cube, dims=("time", "y", "x"), name="precip",
                          coords={"time": pd.date_range(s, e), "y": grid.LATS, "x": grid.LONS},
                          attrs={"units": "mm/day", "source": "SYNTHETIC"})
        da.to_dataset().to_netcdf(clim_dir / f"synthetic_{year}_{s:%m%d}-{e:%m%d}.nc",
                                  encoding={"precip": {"zlib": True, "complevel": 4}})
    summary["clim_years"] = f"{years[0]}-{years[1]}"

    # IMERG-like: 0.1 degree (2x2 block mean, cell centres at x.x5 like IMERG), stored with
    # dimensions (time, lon, lat) as in IMERG V07 daily files.
    src = xr.DataArray(np.stack(series), dims=("time", "y", "x"),
                       coords={"time": pd.to_datetime(days), "y": grid.LATS, "x": grid.LONS})
    coarse = src.coarsen(y=2, x=2).mean()
    coarse = coarse.assign_coords(y=np.round(coarse.y.values, 3), x=np.round(coarse.x.values, 3))
    coarse = coarse * np.exp(0.25 * yr.standard_normal(coarse.shape))      # retrieval "noise"
    imerg = (coarse.rename(y="lat", x="lon").transpose("time", "lon", "lat")
             .astype("float32").rename("precipitation"))
    imerg.attrs["units"] = "mm/day"
    raw = root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    imerg_path = raw / "synthetic_imerg_like_daily.nc"
    imerg.to_dataset().to_netcdf(imerg_path)
    summary["imerg_like"] = str(imerg_path)

    (root / "SYNTHETIC_DATA_README.txt").write_text(
        "Everything under this directory is SYNTHETIC test data generated by ews/synthetic.py.\n"
        "It is not CHIRPS, not IMERG, and the zones are not Uganda's districts.\n"
        + json.dumps(summary, indent=2) + "\n")
    return summary
