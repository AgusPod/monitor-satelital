"""Pipeline diario: calcula indices y lluvia en GEE, escribe site/tiles.json
y descarga un PNG del NDVI nacional para compartir por WhatsApp/LinkedIn."""

import datetime as dt
import json
import os
import pathlib

import ee
import requests


def inicializar():
    clave = os.environ["GEE_SA_KEY"]
    email = os.environ["GEE_SA_EMAIL"]
    proyecto = os.environ["GCP_PROJECT"]
    pathlib.Path("sa_key.json").write_text(clave)
    creds = ee.ServiceAccountCredentials(email, "sa_key.json")
    ee.Initialize(creds, project=proyecto)


HOY = dt.date.today()
VENTANA = 15
FIN = HOY.isoformat()
INICIO = (HOY - dt.timedelta(days=VENTANA)).isoformat()
INICIO_LLUVIA = (HOY - dt.timedelta(days=60)).isoformat()  # CHIRPS/ERA5 llegan con retraso

PALETAS = {
    "NDVI":   {"min": -0.2, "max": 0.9, "palette": ["a50026", "ffffbf", "006837"]},
    "EVI":    {"min": 0.0,  "max": 0.8, "palette": ["ffffcc", "41ab5d", "005a32"]},
    "NDWI":   {"min": -0.5, "max": 0.5, "palette": ["8c510a", "f5f5f5", "01665e"]},
    "MNDWI":  {"min": -0.5, "max": 0.5, "palette": ["8c510a", "f5f5f5", "0571b0"]},
    "lluvia": {"min": 0,    "max": 300, "palette": ["ffffff", "4292c6", "08306b"]},
}


def indices(img):
    ndvi  = img.normalizedDifference(["nir", "red"]).rename("NDVI")
    ndwi  = img.normalizedDifference(["nir", "swir2"]).rename("NDWI")
    mndwi = img.normalizedDifference(["green", "swir1"]).rename("MNDWI")
    evi = img.expression(
        "2.5 * (N - R) / (N + 6*R - 7.5*B + 1)",
        {"N": img.select("nir"), "R": img.select("red"), "B": img.select("blue")},
    ).rename("EVI")
    return img.addBands([ndvi, ndwi, mndwi, evi])


def sentinel2(region):
    def mascara(img):
        scl = img.select("SCL")
        mala = scl.eq(3).Or(scl.eq(8)).Or(scl.eq(9)).Or(scl.eq(10)).Or(scl.eq(11))
        return img.updateMask(mala.Not())

    def preparar(img):
        base = img.select(
            ["B2", "B3", "B4", "B8", "B11", "B12"],
            ["blue", "green", "red", "nir", "swir1", "swir2"],
        ).multiply(0.0001)
        return indices(base)

    return (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(region)
        .filterDate(INICIO, FIN)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
        .map(mascara)
        .map(preparar)
        .median()
    )


def modis(region):
    col = (
        ee.ImageCollection("MODIS/061/MOD13Q1")
        .filterBounds(region)
        .filterDate((HOY - dt.timedelta(days=40)).isoformat(), FIN)
    )
    return col.median().select(["NDVI", "EVI"]).multiply(0.0001)


def lluvias(region):
    chirps = (
        ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
        .filterDate(INICIO_LLUVIA, FIN).select("precipitation")
        .sum().rename("CHIRPS_mm").clip(region)
    )
    era5 = (
        ee.ImageCollection("ECMWF/ERA5_LAND/DAILY_AGGR")
        .filterDate(INICIO_LLUVIA, FIN).select("total_precipitation_sum")
        .sum().multiply(1000).rename("ERA5_mm").clip(region)
    )
    return chirps, era5


def url_tiles(imagen, banda, vis):
    return imagen.select(banda).getMapId(vis)["tile_fetcher"].url_format


def main():
    inicializar()

    arg = (
        ee.FeatureCollection("FAO/GAUL/2015/level2")
        .filter(ee.Filter.eq("ADM0_NAME", "Argentina"))
        .geometry()
    )

    s2 = sentinel2(arg)
    mod = modis(arg)
    chirps, era5 = lluvias(arg)

    tiles = {}
    capas = [
        ("NDVI_s2", s2, "NDVI", PALETAS["NDVI"]),
        ("EVI_s2", s2, "EVI", PALETAS["EVI"]),
        ("NDWI_s2", s2, "NDWI", PALETAS["NDWI"]),
        ("MNDWI_s2", s2, "MNDWI", PALETAS["MNDWI"]),
        ("NDVI_modis", mod, "NDVI", PALETAS["NDVI"]),
        ("lluvia_chirps", chirps, "CHIRPS_mm", PALETAS["lluvia"]),
        ("lluvia_era5", era5, "ERA5_mm", PALETAS["lluvia"]),
    ]
    for nombre, img, banda, vis in capas:
        try:
            tiles[nombre] = url_tiles(img, banda, vis)
            print("capa lista:", nombre)
        except Exception as e:
            print("capa omitida:", nombre, "-", e)

    salida = {"fecha": FIN, "ventana_dias": VENTANA, "tiles": tiles}

    media = (
        mod.select("NDVI")
        .reduceRegion(ee.Reducer.mean(), arg, scale=5000, maxPixels=1e13)
        .get("NDVI").getInfo()
    )
    salida["ndvi_medio_nacional"] = round(media, 3) if media else None

    pathlib.Path("site").mkdir(exist_ok=True)
    pathlib.Path("site/tiles.json").write_text(json.dumps(salida, indent=2))
    print("tiles.json escrito. NDVI medio:", salida["ndvi_medio_nacional"])

    thumb_url = mod.select("NDVI").visualize(**PALETAS["NDVI"]).getThumbURL(
        {"region": arg, "dimensions": 1024, "format": "png"}
    )
    png = requests.get(thumb_url, timeout=300)
    png.raise_for_status()
    pathlib.Path("site/mapa_ndvi.png").write_bytes(png.content)
    print("mapa_ndvi.png descargado:", len(png.content) // 1024, "KB")


if __name__ == "__main__":
    main()
