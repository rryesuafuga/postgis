"""Pixel climatology of N-day rainfall totals for a seasonal window (xarray).

For each baseline year (default 1991-2020) the N-day running totals ending on every day of the
event window +/- `half_window` days are pooled; per pixel, the q-th percentile and the mean of
that sample form the threshold raster. Pooling a seasonal window (rather than the whole year)
keeps the threshold appropriate to the time of year: 95th percentile of July totals, not of
all days including the dry season.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from . import grid
from .fetch import same_day_in_year


def open_inputs(paths: list[Path]) -> xr.DataArray:
    """Concatenate daily NetCDF inputs into one (time, y, x) array on the analysis grid."""
    parts = []
    for p in sorted(paths):
        with xr.open_dataset(p) as ds:
            var = "precip" if "precip" in ds else list(ds.data_vars)[0]
            parts.append(grid.to_grid(ds[var].load()))
    da = xr.concat(parts, dim="time").sortby("time")
    da["time"] = pd.to_datetime(da["time"].values).normalize()
    _, keep = np.unique(da["time"].values, return_index=True)
    return da.isel(time=np.sort(keep))


def accumulation_sample(daily: xr.DataArray, event_start: date, event_end: date,
                        years: range, half_window: int, accum_days: int = 3) -> xr.DataArray:
    """All N-day totals ending inside the seasonal window, pooled over `years`."""
    samples = []
    span = event_end - event_start
    for year in years:
        first_end = same_day_in_year(event_start, year) - timedelta(days=half_window)
        last_end = first_end + span + timedelta(days=2 * half_window)
        need = pd.date_range(first_end - timedelta(days=accum_days - 1), last_end)
        sub = daily.reindex(time=need)                        # gaps -> NaN, never bridged
        acc = sub.rolling(time=accum_days, min_periods=accum_days).sum()
        samples.append(acc.sel(time=slice(pd.Timestamp(first_end), pd.Timestamp(last_end))))
    return xr.concat(samples, dim="time")


def build(daily: xr.DataArray, event_start: date, event_end: date, years: tuple[int, int],
          half_window: int = 15, accum_days: int = 3, q: float = 0.95,
          min_coverage: float = 0.8) -> tuple[np.ndarray, np.ndarray, dict]:
    """Return (percentile, mean, metadata) for the window; pixels with < min_coverage -> NaN."""
    available = sorted(set(pd.DatetimeIndex(daily["time"].values).year))
    yrs = range(years[0], years[1] + 1)
    missing = [y for y in yrs if y not in available]
    sample = accumulation_sample(daily, event_start, event_end, yrs, half_window, accum_days)
    valid = sample.notnull().sum("time")
    expected = sample.sizes["time"]
    pct = sample.quantile(q, dim="time", skipna=True).values.astype("float32")
    mean = sample.mean("time", skipna=True).values.astype("float32")
    poor = valid.values < min_coverage * expected
    pct[poor] = np.nan
    mean[poor] = np.nan
    meta = {
        "stat": f"p{round(q * 100)}", "accum_days": accum_days, "base_years": f"{years[0]}-{years[1]}",
        "missing_years": missing, "half_window_days": half_window,
        "window_start": (event_start - timedelta(days=half_window)).strftime("%m%d"),
        "window_end": (event_end + timedelta(days=half_window)).strftime("%m%d"),
        "samples_per_pixel": int(expected),
        "pixels_masked_low_coverage": int(poor.sum()),
    }
    return pct, mean, meta


def output_name(meta: dict, synthetic: bool = False) -> str:
    y0, y1 = meta["base_years"].split("-")
    suffix = "_synthetic" if synthetic else ""
    return (f"clim_{meta['stat']}_{meta['accum_days']}d_{y0}-{y1}_"
            f"{meta['window_start']}-{meta['window_end']}{suffix}.tif")


def build_to_geotiff(input_paths: list[Path], out_dir: Path, event_start: date, event_end: date,
                     years: tuple[int, int], half_window: int, accum_days: int = 3,
                     q: float = 0.95) -> Path:
    if not input_paths:
        raise FileNotFoundError("no climatology inputs: run `python -m ews fetch-climatology` first")
    daily = open_inputs(input_paths)
    pct, mean, meta = build(daily, event_start, event_end, years, half_window, accum_days, q)
    if meta["missing_years"]:
        raise ValueError(f"baseline years missing from inputs: {meta['missing_years']}")
    synthetic = any("synthetic" in p.name for p in input_paths)
    out = out_dir / output_name(meta, synthetic)
    grid.write_geotiff(out, [pct, mean],
                       tags={**meta, "product": "SYNTHETIC" if synthetic else "CHIRPS v2.0",
                             "units": "mm"},
                       descriptions=[f"{meta['stat']} of {accum_days}-day totals",
                                     f"mean of {accum_days}-day totals"])
    return out
