from datetime import date

import numpy as np
import pandas as pd
import xarray as xr

from ews import climatology, grid
from ews.fetch import clim_window


def cube(start: str, end: str, value_fn) -> xr.DataArray:
    t = pd.date_range(start, end)
    data = np.stack([np.full((grid.HEIGHT, grid.WIDTH), value_fn(d), "float32") for d in t])
    return xr.DataArray(data, dims=("time", "y", "x"), coords={"time": t, "y": grid.LATS, "x": grid.LONS})


def test_clim_window_covers_running_totals():
    s, e = clim_window(date(2022, 7, 20), date(2022, 8, 5), 2001, half_window=15, accum_days=3)
    assert s == date(2001, 7, 3)        # 20 Jul - 15 d - 2 lead days
    assert e == date(2001, 8, 20)       # 5 Aug + 15 d


def test_percentile_of_three_day_totals():
    # Daily rain = day index within the year's window, so 3-day totals are known exactly.
    parts = []
    for year in (2001, 2002):
        s, e = clim_window(date(2022, 7, 20), date(2022, 7, 22), year, half_window=1)
        parts.append(cube(str(s), str(e), lambda d, s=s: float((d.date() - s).days)))
    daily = xr.concat(parts, dim="time")
    pct, mean, meta = climatology.build(daily, date(2022, 7, 20), date(2022, 7, 22), (2001, 2002),
                                        half_window=1, q=0.95)
    # windows end on 19..23 Jul -> day indices 2..6; total = 3i - 3 -> {3, 6, 9, 12, 15} each year
    totals = np.array([3, 6, 9, 12, 15] * 2, dtype=float)
    assert np.allclose(pct, np.quantile(totals, 0.95))
    assert np.allclose(mean, totals.mean())
    assert meta["window_start"] == "0719" and meta["window_end"] == "0723"
    assert meta["samples_per_pixel"] == 10


def test_gaps_are_not_bridged():
    s, e = clim_window(date(2022, 7, 20), date(2022, 7, 22), 2001, half_window=1)
    daily = cube(str(s), str(e), lambda d: 1.0)
    daily = daily.drop_sel(time=pd.Timestamp(2001, 7, 21))      # one missing day
    sample = climatology.accumulation_sample(daily, date(2022, 7, 20), date(2022, 7, 22), range(2001, 2002), 1)
    valid = sample.isel(y=0, x=0).notnull().values
    # totals ending 21, 22, 23 Jul include the missing day -> NaN; 19 and 20 Jul are complete
    assert valid.tolist() == [True, True, False, False, False]


def test_low_coverage_pixels_masked():
    s, e = clim_window(date(2022, 7, 20), date(2022, 7, 22), 2001, half_window=1)
    daily = cube(str(s), str(e), lambda d: 2.0)
    daily[:, 0, 0] = np.nan
    pct, _, meta = climatology.build(daily, date(2022, 7, 20), date(2022, 7, 22), (2001, 2001), half_window=1)
    assert np.isnan(pct[0, 0]) and np.isclose(pct[1, 1], 6.0)
    assert meta["pixels_masked_low_coverage"] == 1
