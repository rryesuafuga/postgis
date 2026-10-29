"""SYNTHETIC rainfall for running the whole pipeline offline, over REAL district boundaries.

The districts are real: a simplified copy of the geoBoundaries gbOpen Uganda ADM3 layer
(137 districts, CC0; see ews/fixtures/README.md). The rainfall is not an observation. It exists
so the loaders, SQL, CAP export, integrity checks, backups and the map can be exercised without
network access to CHC/ERDDAP. Every rainfall artefact is labelled: file names start with
`synthetic`, rasters carry product "SYNTHETIC", and the CAP exporter forces status=Test.

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

from . import config, grid
from .fetch import clim_window, daterange, write_source_env

FIXTURES = Path(__file__).parent / "fixtures"
DISTRICTS = FIXTURES / "uga_districts_geoboundaries_adm3.geojson"

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


def _flush(arr: np.ndarray) -> np.ndarray:
    """Amounts below 0.01 mm become 0, as in gauge and satellite products (and no denormals)."""
    return np.where(arr < 0.01, 0.0, arr).astype("float32")


def storm_rain(day: date) -> np.ndarray:
    add = np.zeros((grid.HEIGHT, grid.WIDTH), "float32")
    for lon0, lat0, sigma, peaks in STORMS:
        peak = peaks.get(f"{day:%m-%d}")
        if peak:
            add += peak * _bump(lon0, lat0, sigma)
    return add


def use_districts(boundary_dir: Path) -> Path:
    """Point the district loader at the vendored geoBoundaries district layer."""
    boundary_dir.mkdir(parents=True, exist_ok=True)
    write_source_env(boundary_dir, str(DISTRICTS.relative_to(config.ROOT)), None,
                     "geoBoundaries gbOpen UGA ADM3 (District), CC0; simplified copy",
                     "GEOBOUNDARIES-ID", 136, name="shapename", code="shapeid")   # 137 minus Lake Victoria
    return DISTRICTS


def make_all(root: Path, event_start: date, event_end: date, years: tuple[int, int],
             half_window: int, seed: int = 20220730) -> dict:
    rng = np.random.default_rng(seed)
    mean = mean_field()
    summary = {"root": str(root), "seed": seed}

    summary["districts"] = str(use_districts(root / "boundaries"))

    daily_dir = root / "chirps" / "daily"
    days = list(daterange(event_start - timedelta(days=2), event_end))
    series = []
    for d in days:
        arr = _flush(daily_field(rng, mean) + storm_rain(d))
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
        cube = np.stack([_flush(daily_field(yr, mean)) for _ in daterange(s, e)])
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
        "The rainfall under this directory is SYNTHETIC test data generated by ews/synthetic.py.\n"
        "It is not CHIRPS and not IMERG. The district boundaries are real (geoBoundaries ADM3).\n"
        + json.dumps(summary, indent=2) + "\n")
    return summary
