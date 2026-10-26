"""Downloaders exercised against local fakes shaped like the real products (no network)."""

import gzip
import shlex
import shutil
import zipfile
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import pytest
import rasterio
import xarray as xr
from rasterio.transform import from_origin
from shapely.geometry import box

from ews import fetch, grid


def serve(src: Path):
    """A stand-in for fetch.download that serves a local file."""
    def fake_download(url, dest, session=None, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest)
        return dest
    return fake_download


def read_env(path: Path) -> dict:
    out = {}
    for line in path.read_text().splitlines():
        k, v = line.split("=", 1)
        out[k] = shlex.split(v)[0] if v else ""
    return out


def test_chc_africa_tif_is_cropped_without_resampling(tmp_path, monkeypatch):
    # CHIRPS Africa daily: 1500 x 1600 cells of 0.05 deg from (-20 E, 40 N); value = column index.
    src = tmp_path / "africa.tif"
    cols = np.tile(np.arange(1500, dtype="float32"), (1600, 1))
    with rasterio.open(src, "w", driver="GTiff", width=1500, height=1600, count=1, dtype="float32",
                       crs="EPSG:4326", transform=from_origin(-20, 40, 0.05, 0.05), nodata=-9999) as d:
        d.write(cols, 1)
    gz = tmp_path / "africa.tif.gz"
    with open(src, "rb") as fi, gzip.open(gz, "wb") as fo:
        shutil.copyfileobj(fi, fo)
    seen = []

    def fake_download(url, dest, session=None, **kw):
        seen.append(url)
        shutil.copy(gz, dest)
        return dest

    monkeypatch.setattr(fetch, "download", fake_download)
    out = fetch.fetch_chirps_day_chc(date(2022, 7, 30), tmp_path / "out")
    assert seen[0].endswith("africa_daily/tifs/p05/2022/chirps-v2.0.2022.07.30.tif.gz")
    with rasterio.open(out) as d:
        assert d.transform.almost_equals(grid.TRANSFORM) and (d.width, d.height) == (grid.WIDTH, grid.HEIGHT)
        arr = d.read(1)
    first_col = round((grid.WEST + 20) / 0.05)          # 988
    assert np.array_equal(arr[0], np.arange(first_col, first_col + grid.WIDTH, dtype="float32"))


def test_erddap_subset_is_put_north_up(tmp_path, monkeypatch):
    lats = np.round(np.arange(grid.SOUTH + 0.025, grid.NORTH, 0.05), 3)       # ascending, like ERDDAP
    lons = np.round(np.arange(grid.WEST + 0.025, grid.EAST, 0.05), 3)
    t = pd.date_range("2022-07-29", "2022-07-30")
    vals = np.broadcast_to(lats[None, :, None] * 100, (2, lats.size, lons.size)).astype("float32")
    nc = tmp_path / "erddap.nc"
    xr.DataArray(vals, dims=("time", "latitude", "longitude"), name="precip",
                 coords={"time": t, "latitude": lats, "longitude": lons}).to_dataset().to_netcdf(nc)

    def fake_download(url, dest, session=None, **kw):
        assert "chirps20GlobalDailyP05.nc?precip" in url
        shutil.copy(nc, dest)
        return dest

    monkeypatch.setattr(fetch, "download", fake_download)
    files = fetch.fetch_chirps(date(2022, 7, 29), date(2022, 7, 30), tmp_path / "daily", source="erddap")
    arr = grid.read_geotiff(files[0])
    assert np.isclose(arr[0, 0], grid.LATS[0] * 100) and np.isclose(arr[-1, 0], grid.LATS[-1] * 100)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    headers: dict = {}

    def __init__(self, payload):
        self.payload = payload

    def get(self, url, timeout=None):
        return FakeResponse(self.payload)


def test_hdx_cod_ab_admin2_shapefile_in_zip(tmp_path, monkeypatch):
    shp_dir = tmp_path / "shp"
    shp_dir.mkdir()
    gdf = gpd.GeoDataFrame({"ADM2_EN": ["Bududa", "Mbale"], "ADM2_PCODE": ["UG3020", "UG3021"],
                            "ADM1_EN": ["Eastern", "Eastern"]},
                           geometry=[box(34.2, 0.9, 34.5, 1.2), box(34.0, 0.9, 34.2, 1.2)], crs="EPSG:4326")
    for name in ("uga_admbnda_adm1_ubos_20200824", "uga_admbnda_adm2_ubos_20200824"):
        gdf.to_file(shp_dir / f"{name}.shp")
    zpath = tmp_path / "uga_admbnda_ubos_20200824_shp.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        for f in shp_dir.iterdir():
            z.write(f, f.name)
    payload = {"result": {"resources": [
        {"name": "uga_admbnda_ubos_20200824.xlsx", "format": "XLSX", "url": "https://x/y.xlsx"},
        {"name": "uga_admbnda_ubos_20200824_shp.zip", "format": "SHP",
         "url": "https://data.humdata.org/dataset/abc/resource/def/download/uga_admbnda_ubos_20200824_shp.zip"},
    ]}}
    monkeypatch.setattr(fetch, "_session", lambda: FakeSession(payload))
    monkeypatch.setattr(fetch, "download", serve(zpath))
    env = read_env(fetch.fetch_districts_hdx(tmp_path / "boundaries"))
    assert env["EWS_DISTRICT_SRC"].startswith("/vsizip/") and "adm2" in env["EWS_DISTRICT_SRC"]
    assert env["EWS_DISTRICT_CODE_SCHEME"] == "OCHA-PCODE"
    assert pyogrio.read_info(env["EWS_DISTRICT_SRC"], layer=env["EWS_DISTRICT_LAYER"])["features"] == 2


def test_gadm_district_layer_chosen_by_count(tmp_path, monkeypatch):
    gpkg = tmp_path / "gadm41_UGA.gpkg"
    for layer, n in (("ADM_ADM_0", 1), ("ADM_ADM_1", 135), ("ADM_ADM_2", 400)):
        g = gpd.GeoDataFrame({f"GID_{layer[-1]}": [f"UGA.{i}" for i in range(n)],
                              f"NAME_{layer[-1]}": [f"n{i}" for i in range(n)]},
                             geometry=[box(30 + i * 0.001, 0, 30 + i * 0.001 + 0.0005, 0.0005) for i in range(n)],
                             crs="EPSG:4326")
        g.to_file(gpkg, layer=layer, driver="GPKG")
    monkeypatch.setattr(fetch, "download", serve(gpkg))
    env = read_env(fetch.fetch_districts_gadm(tmp_path / "boundaries"))
    assert env["EWS_DISTRICT_LAYER"] == "ADM_ADM_1"
    assert env["EWS_DISTRICT_NAME_FIELD"] == "name_1" and env["EWS_DISTRICT_CODE_FIELD"] == "gid_1"
    assert env["EWS_EXPECTED_DISTRICTS"] == "135"


def test_imerg_time_lon_lat_order_is_transposed(tmp_path):
    lon = np.round(np.arange(29.05, 35.6, 0.1), 2)
    lat = np.round(np.arange(-1.95, 4.8, 0.1), 2)
    t = pd.date_range("2022-07-30", periods=1)
    # field increases northwards only: any transposition or flip shows up immediately
    vals = np.broadcast_to(lat[None, None, :], (1, lon.size, lat.size)).astype("float32") + 10
    nc = tmp_path / "3B-DAY.MS.MRG.3IMERG.20220730-S000000-E235959.V07B.nc4"
    xr.DataArray(vals, dims=("time", "lon", "lat"), name="precipitation",
                 coords={"time": t, "lon": lon, "lat": lat}).to_dataset().to_netcdf(nc)
    files = fetch.prep_aux(nc, "imerg", tmp_path / "aux")
    arr = grid.read_geotiff(files[0])
    assert files[0].name == "imerg.2022.07.30.tif"
    assert arr[0, 50] > arr[-1, 50]                                  # north is up
    assert np.allclose(arr[:, 10], arr[:, 80], atol=1e-4)           # no east-west structure
    assert np.isclose(arr[60, 60], grid.LATS[60] + 10, atol=0.06)


@pytest.mark.parametrize("day,year,expected", [(date(2024, 2, 29), 2001, date(2001, 2, 28)),
                                               (date(2022, 7, 30), 1991, date(1991, 7, 30))])
def test_same_day_in_year(day, year, expected):
    assert fetch.same_day_in_year(day, year) == expected
