"""The analysis grid: CHIRPS 0.05 degree cells over Uganda.

Every raster that reaches the database (daily CHIRPS, climatology, auxiliary layers) is put on
this exact grid first, so tiles produced by raster2pgsql line up one-to-one and raster
constraints (same alignment, extent) hold. Edges sit on multiples of 0.05 degrees, matching the
CHIRPS Africa/global grids, so CHIRPS subsets need no resampling at all.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
import xarray as xr
from rasterio.transform import from_origin

RES = 0.05
WEST, SOUTH, EAST, NORTH = 29.40, -1.60, 35.20, 4.40   # Uganda (29.57..34.98E, 1.47S..4.22N) + margin
WIDTH = round((EAST - WEST) / RES)    # 116
HEIGHT = round((NORTH - SOUTH) / RES)  # 120
TRANSFORM = from_origin(WEST, NORTH, RES, RES)
NODATA = -9999.0
CRS = "EPSG:4326"

LONS = np.round(WEST + RES * (np.arange(WIDTH) + 0.5), 6)
LATS = np.round(NORTH - RES * (np.arange(HEIGHT) + 0.5), 6)   # north -> south (raster order)


def _rename_xy(da: xr.DataArray) -> xr.DataArray:
    names = {}
    for d in da.dims:
        low = d.lower()
        if low in ("lon", "longitude", "x"):
            names[d] = "x"
        elif low in ("lat", "latitude", "y"):
            names[d] = "y"
    da = da.rename(names)
    if "x" not in da.dims or "y" not in da.dims:
        raise ValueError(f"cannot identify lon/lat dimensions in {da.dims}")
    # Some products (e.g. GPM IMERG) store (time, lon, lat): put y, x last.
    other = [d for d in da.dims if d not in ("y", "x")]
    return da.transpose(*other, "y", "x")


def to_grid(da: xr.DataArray) -> xr.DataArray:
    """Return `da` on the analysis grid.

    Inputs already on the 0.05 degree CHIRPS lattice are subset exactly (nearest neighbour
    with a tolerance far below one cell). Anything else (e.g. IMERG 0.1 degree) is resampled
    with rioxarray: bilinear for continuous fields.
    """
    da = _rename_xy(da)
    xs, ys = da["x"].values, da["y"].values
    dx = float(np.median(np.abs(np.diff(xs)))) if xs.size > 1 else 0.0

    def centred(v: np.ndarray, origin: float) -> bool:   # cell centres at origin + (k + 0.5) * RES
        k = (v - origin) / RES - 0.5
        return bool(np.all(np.abs(k - np.round(k)) < 1e-3))

    on_lattice = abs(dx - RES) < 1e-6 and centred(xs, WEST) and centred(ys, SOUTH)
    if on_lattice:
        da = da.sortby("y", ascending=False).sortby("x")
        out = da.reindex(y=LATS, x=LONS, method="nearest", tolerance=RES / 10)
    else:
        import rioxarray  # noqa: F401  (registers .rio)
        from rasterio.enums import Resampling

        da = da.rio.set_spatial_dims(x_dim="x", y_dim="y").rio.write_crs(CRS)
        target = xr.DataArray(np.zeros((HEIGHT, WIDTH), np.float32),
                              coords={"y": LATS, "x": LONS}, dims=("y", "x"))
        target = target.rio.write_crs(CRS).rio.write_transform(TRANSFORM)
        out = da.rio.reproject_match(target, resampling=Resampling.bilinear)
        out = out.where(out != out.rio.nodata) if out.rio.nodata is not None else out
        out = out.assign_coords(y=LATS, x=LONS)
    return out.astype("float32")


def write_geotiff(path: Path, bands: np.ndarray | list[np.ndarray], tags: dict | None = None,
                  descriptions: list[str] | None = None) -> Path:
    """Write one or more (HEIGHT, WIDTH) float arrays on the analysis grid; NaN -> NODATA."""
    arrays = [bands] if isinstance(bands, np.ndarray) and bands.ndim == 2 else list(bands)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", width=WIDTH, height=HEIGHT, count=len(arrays),
                       dtype="float32", crs=CRS, transform=TRANSFORM, nodata=NODATA,
                       compress="deflate", predictor=3, tiled=False) as dst:
        for i, arr in enumerate(arrays, start=1):
            a = np.asarray(arr, dtype="float32")
            if a.shape != (HEIGHT, WIDTH):
                raise ValueError(f"band {i} shape {a.shape} != grid {(HEIGHT, WIDTH)}")
            dst.write(np.where(np.isfinite(a), a, NODATA).astype("float32"), i)
            if descriptions:
                dst.set_band_description(i, descriptions[i - 1])
        if tags:
            dst.update_tags(**{k: str(v) for k, v in tags.items()})
    return path


def read_geotiff(path: Path, band: int = 1) -> np.ndarray:
    with rasterio.open(path) as src:
        a = src.read(band).astype("float32")
        if src.nodata is not None:
            a[a == src.nodata] = np.nan
    return a
