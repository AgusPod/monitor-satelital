"""Pipeline diario: calcula indices y lluvia en GEE, escribe site/tiles.json
y descarga un PNG del NDVI nacional para compartir por WhatsApp/LinkedIn.

Modulos:
  - Indices opticos (Sentinel-2, MODIS) y lluvias acumuladas (CHIRPS, ERA5)
  - Reporte de granizo del dia anterior (GOES-19, tope de nube < -58 C)
  - Reporte de heladas de la ultima madrugada disponible (ERA5-Land horario):
    temperatura minima a 2 m, humedad relativa, cielo despejado (GOES) y
    temperatura minima por departamento para el aviso agronomico del dashboard
  - Reporte de inundaciones: lluvia 72 h (GPM IMERG), agua detectada por radar
    (Sentinel-1, funciona con nubes), agua nueva (fuera de cuerpos permanentes
    JRC) y zonas bajas anegables (HAND de MERIT Hydro)
"""

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
    "tmin":   {"min": -8,   "max": 10,  "palette": ["7f00ff", "0000ff", "00bfff", "ffffff", "ffff99", "ff8c00"]},
    "hr":     {"min": 20,   "max": 100, "palette": ["d73027", "fee090", "e0f3f8", "4575b4"]},
    "imerg":  {"min": 0,    "max": 120, "palette": ["ffffff", "74c476", "2171b5", "6a51a3", "cb181d"]},
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
        .filterDate((HOY - dt.timedelta(days=80)).isoformat(), FIN)
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


def temperatura_goes(img):
    """CMI_C13 (10.3 um) a temperatura de brillo en Kelvin."""
    esc = ee.Number(img.get("CMI_C13_scale"))
    off = ee.Number(img.get("CMI_C13_offset"))
    return img.select("CMI_C13").multiply(esc).add(off).rename("T")


def reporte_granizo(region, departamentos):
    """Revisa todas las imagenes GOES-19 de ayer y marca donde el tope de nube
    bajo de -58 C (215 K): conveccion severa con posible granizo."""
    ayer = (HOY - dt.timedelta(days=1)).isoformat()
    col = ee.ImageCollection("NOAA/GOES/19/MCMIPF").filterDate(ayer, FIN)

    tmin = col.map(temperatura_goes).min().clip(region)
    stats = tmin.reduceRegions(collection=departamentos, reducer=ee.Reducer.min(), scale=4000)
    afectados = stats.filter(ee.Filter.lt("min", 215))
    lista = afectados.reduceColumns(
        ee.Reducer.toList(3), ["ADM2_NAME", "ADM1_NAME", "min"]
    ).get("list").getInfo()
    url = url_tiles(
        tmin.updateMask(tmin.lt(215)), "T",
        {"min": 185, "max": 215, "palette": ["ff00ff", "ff0000", "ffa500", "ffff00"]},
    )
    def severidad(t):
        if t <= -70:
            return "granizo grande probable"
        if t <= -65:
            return "granizo probable"
        return "posible granizo"

    deps = [
        {
            "departamento": x[0], "provincia": x[1],
            "tmin_c": round(x[2] - 273.15, 1),
            "severidad": severidad(x[2] - 273.15),
        }
        for x in lista
    ]
    deps.sort(key=lambda d: d["tmin_c"])
    return {"fecha": ayer, "umbral_c": -58, "departamentos": deps}, url


def reporte_heladas(region, departamentos):
    """Heladas de la ultima madrugada disponible en ERA5-Land (horario).

    ERA5-Land llega con ~5 dias de retraso; se toma la ultima madrugada
    completa disponible y se declara la fecha honestamente en el JSON.
    Ventana: 03 a 13 UTC (00 a 10 hora argentina), que cubre la minima
    del amanecer. Devuelve:
      - tile de temperatura minima a 2 m
      - tile de humedad relativa minima de la madrugada
      - temperatura minima por departamento (para el aviso agronomico,
        el umbral por cultivo se aplica en el dashboard)
      - fraccion de cielo cubierto esa noche segun GOES (el enfriamiento
        radiativo que causa helada necesita cielo despejado)
    """
    col = ee.ImageCollection("ECMWF/ERA5_LAND/HOURLY").select(
        ["temperature_2m", "dewpoint_temperature_2m"]
    )
    ultima = ee.Date(
        col.filterDate((HOY - dt.timedelta(days=15)).isoformat(), FIN)
        .limit(1, "system:time_start", False).first().get("system:time_start")
    )
    # Si la ultima imagen es anterior a las 13 UTC, la madrugada completa es la del dia previo
    fecha = ee.Date(ee.Algorithms.If(ultima.get("hour").lt(13), ultima.advance(-1, "day"), ultima))
    fecha_str = fecha.format("YYYY-MM-dd").getInfo()
    ini = ee.Date(fecha_str).advance(3, "hour")
    fin = ee.Date(fecha_str).advance(13, "hour")

    madrugada = col.filterDate(ini, fin)

    def celsius(img):
        t = img.select("temperature_2m").subtract(273.15).rename("tmin")
        td = img.select("dewpoint_temperature_2m").subtract(273.15)
        # Humedad relativa por formula de Magnus
        hr = td.multiply(17.625).divide(td.add(243.04)).exp().divide(
            t.multiply(17.625).divide(t.add(243.04)).exp()
        ).multiply(100).rename("hr")
        return t.addBands(hr)

    horas = madrugada.map(celsius)
    tmin = horas.select("tmin").min().clip(region)
    hr_min = horas.select("hr").min().clip(region)

    # Nubosidad nocturna GOES (1 imagen por hora): 0 = despejado, 1 = cubierto
    try:
        goes = (
            ee.ImageCollection("NOAA/GOES/19/MCMIPF")
            .filterDate(ini, fin)
            .filter(ee.Filter.calendarRange(0, 9, "minute"))
        )
        nubosidad = goes.map(lambda i: temperatura_goes(i).lt(283)).mean().rename("nub").clip(region)
    except Exception:
        nubosidad = ee.Image.constant(-1).rename("nub").clip(region)

    combinado = tmin.rename("tmin").addBands(hr_min.rename("hr")).addBands(nubosidad)
    reducer = ee.Reducer.min().combine(ee.Reducer.mean(), sharedInputs=True)
    stats = combinado.reduceRegions(collection=departamentos, reducer=reducer, scale=11000)
    lista = stats.reduceColumns(
        ee.Reducer.toList(5), ["ADM2_NAME", "ADM1_NAME", "tmin_min", "hr_min", "nub_mean"]
    ).get("list").getInfo()

    deps = []
    for x in lista:
        if x[2] is None:
            continue
        deps.append({
            "departamento": x[0],
            "provincia": x[1],
            "tmin_c": round(x[2], 1),
            "hr_pct": round(x[3]) if x[3] is not None else None,
            "despejado": (x[4] is not None and 0 <= x[4] < 0.35),
        })
    deps.sort(key=lambda d: d["tmin_c"])

    url_tmin = url_tiles(tmin, "tmin", PALETAS["tmin"])
    url_hr = url_tiles(hr_min, "hr", PALETAS["hr"])
    reporte = {
        "fecha": fecha_str,
        "ventana_utc": "03 a 13 UTC (00 a 10 hora argentina)",
        "fuente": "ERA5-Land horario (llega con ~5 dias de retraso)",
        "departamentos": deps,
    }
    return reporte, url_tmin, url_hr


def reporte_inundaciones(region, departamentos):
    """Riesgo y deteccion de inundaciones (metodologia tipo ARSET/NASA):
      - Lluvia acumulada de 72 h con GPM IMERG (casi tiempo real)
      - Agua en superficie con radar Sentinel-1 (VV < -16 dB, ve a traves de nubes)
      - Agua nueva: la que no figura como cuerpo permanente en JRC Global Surface Water
      - Zonas bajas anegables: HAND < 5 m (MERIT Hydro), capa estatica de riesgo
    """
    ini72 = (HOY - dt.timedelta(days=3)).isoformat()
    imerg = (
        ee.ImageCollection("NASA/GPM_L3/IMERG_V07")
        .filterDate(ini72, FIN)
        .select("precipitation")
    )
    # mm/h cada media hora -> mm acumulados
    lluvia72 = imerg.sum().multiply(0.5).rename("mm72").clip(region)

    s1 = (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filterBounds(region)
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .select("VV")
    )
    vv_actual = s1.filterDate((HOY - dt.timedelta(days=12)).isoformat(), FIN).median()
    agua = vv_actual.lt(-16).selfMask().rename("agua").clip(region)

    hand = ee.Image("MERIT/Hydro/v1_0_1").select("hnd")
    zonas_bajas = hand.lt(5).selfMask().rename("bajas").clip(region)

    permanente = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("occurrence").unmask(0).gte(50)
    agua_nueva = (
        vv_actual.lt(-16).And(permanente.Not()).And(hand.lt(10))
        .selfMask().rename("nueva").clip(region)
    )

    tiles = {
        "lluvia_imerg72": url_tiles(lluvia72, "mm72", PALETAS["imerg"]),
        "agua_s1": url_tiles(agua, "agua", {"min": 0, "max": 1, "palette": ["0ea5e9"]}),
        "agua_nueva": url_tiles(agua_nueva, "nueva", {"min": 0, "max": 1, "palette": ["f43f5e"]}),
        "zonas_bajas": url_tiles(zonas_bajas, "bajas", {"min": 0, "max": 1, "palette": ["a78bfa"]}),
    }

    # Estadistica por departamento: hectareas de agua nueva + lluvia maxima de 72 h
    area_ha = agua_nueva.unmask(0).multiply(ee.Image.pixelArea()).divide(10000).rename("ha")
    combinado = area_ha.addBands(lluvia72.rename("mm72"))
    reducer = ee.Reducer.sum().combine(ee.Reducer.max(), sharedInputs=True)
    stats = combinado.reduceRegions(collection=departamentos, reducer=reducer, scale=300)
    lista = stats.reduceColumns(
        ee.Reducer.toList(4), ["ADM2_NAME", "ADM1_NAME", "ha_sum", "mm72_max"]
    ).get("list").getInfo()

    deps = []
    for x in lista:
        ha = round(x[2]) if x[2] is not None else 0
        mm = round(x[3]) if x[3] is not None else 0
        if ha >= 200 or mm >= 80:
            deps.append({
                "departamento": x[0], "provincia": x[1],
                "agua_nueva_ha": ha, "lluvia_72h_mm": mm,
            })
    deps.sort(key=lambda d: -d["agua_nueva_ha"])

    reporte = {
        "fecha": FIN,
        "criterio": "agua nueva >= 200 ha (Sentinel-1, ultimos 12 dias) o lluvia 72 h >= 80 mm (IMERG)",
        "departamentos": deps[:40],
    }
    return reporte, tiles


def capas_topografia(region):
    """Topografia desde Copernicus DEM GLO-30 (30 m), la mejor base global.
    'Lomas y bajos' es la elevacion relativa al promedio del entorno de 1.5 km:
    resalta el microrelieve que define donde se junta el agua, aunque el
    desnivel total sea de pocos metros. Son capas estaticas (no cambian por
    dia), pero se regeneran porque las URLs de tiles de GEE caducan."""
    dem = (
        ee.ImageCollection("COPERNICUS/DEM/GLO30")
        .select("DEM").mosaic()
        .setDefaultProjection("EPSG:4326", None, 30)
        .clip(region)
    )
    pendiente = ee.Terrain.slope(dem).rename("pend")
    sombra = ee.Terrain.hillshade(dem).rename("sombra")
    relativo = dem.subtract(dem.focalMean(1500, "circle", "meters")).rename("rel")

    return {
        "topo_elevacion": url_tiles(
            dem.rename("elev"), "elev",
            {"min": -50, "max": 3000,
             "palette": ["0a7e2e", "7fbf4d", "f7e08b", "c9a15f", "8a6a42", "f2f2f2"]},
        ),
        "topo_pendiente": url_tiles(
            pendiente, "pend",
            {"min": 0, "max": 15,
             "palette": ["ffffff", "fdd49e", "fc8d59", "d7301f", "7f0000"]},
        ),
        "topo_sombreado": url_tiles(
            sombra, "sombra", {"min": 0, "max": 255, "palette": ["000000", "ffffff"]},
        ),
        "topo_relieve": url_tiles(
            relativo, "rel",
            {"min": -3, "max": 3,
             "palette": ["313695", "74add1", "f7f7f7", "f46d43", "a50026"]},
        ),
    }


def actualizar_historial(nombre, registro, clave_fecha="fecha", maximo=60):
    path = pathlib.Path("site/" + nombre)
    historial = json.loads(path.read_text()) if path.exists() else []
    historial = [h for h in historial if h.get(clave_fecha) != registro[clave_fecha]]
    historial.append(registro)
    historial = historial[-maximo:]
    path.write_text(json.dumps(historial, indent=2))


def ambientar_lotes():
    """K-means de ambientacion por lote guardado (site/lotes.json).
    Zonas de vigor desde una mediana estacional de NDVI (Sentinel-2, ~150 dias,
    enmascarado de nubes). Escribe site/lotes_data.json con el tile de zonas,
    hectareas y NDVI medio por zona, y una serie mensual de NDVI del lote."""
    path_lotes = pathlib.Path("site/lotes.json")
    if not path_lotes.exists():
        return
    lotes = json.loads(path_lotes.read_text()).get("lotes", [])
    salida = {"fecha": FIN, "lotes": []}

    ini_amb = (HOY - dt.timedelta(days=150)).isoformat()
    n_zonas = 3
    paleta = ["d73027", "fee08b", "1a9850"]

    def _ndvi_s2(img):
        scl = img.select("SCL")
        mala = scl.eq(3).Or(scl.eq(8)).Or(scl.eq(9)).Or(scl.eq(10)).Or(scl.eq(11))
        return img.normalizedDifference(["B8", "B4"]).rename("NDVI").updateMask(mala.Not())

    for lote in lotes:
        try:
            geom = ee.Geometry(lote["geometry"])
            col = (
                ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                .filterBounds(geom).filterDate(ini_amb, FIN)
                .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
            )
            ndvi_med = col.map(_ndvi_s2).median().rename("NDVI").clip(geom)

            muestras = ndvi_med.sample(region=geom, scale=10, numPixels=1000)
            clusterer = ee.Clusterer.wekaKMeans(n_zonas).train(muestras)
            clasificado = ndvi_med.cluster(clusterer).rename("zona")

            medios = ndvi_med.addBands(clasificado).reduceRegion(
                reducer=ee.Reducer.mean().group(groupField=1, groupName="zona"),
                geometry=geom, scale=10, maxPixels=1e9,
            ).get("groups").getInfo()
            orden = sorted(medios, key=lambda g: g["mean"])
            remap_from = [int(g["zona"]) for g in orden]
            remap_to = list(range(len(orden)))
            zonas_img = clasificado.remap(remap_from, remap_to).rename("zona").clip(geom)

            areas = ee.Image.pixelArea().divide(10000).addBands(zonas_img).reduceRegion(
                reducer=ee.Reducer.sum().group(groupField=1, groupName="zona"),
                geometry=geom, scale=10, maxPixels=1e9,
            ).get("groups").getInfo()
            area_por_zona = {int(a["zona"]): a["sum"] for a in areas}

            zonas = []
            for i, g in enumerate(orden):
                zonas.append({
                    "zona": i,
                    "ndvi_medio": round(g["mean"], 3),
                    "area_ha": round(area_por_zona.get(i, 0), 1),
                })

            tile = url_tiles(zonas_img, "zona", {"min": 0, "max": n_zonas - 1, "palette": paleta})

            serie = []
            for m in range(11, -1, -1):
                fin_m = HOY - dt.timedelta(days=30 * m)
                ini_m = fin_m - dt.timedelta(days=30)
                col_m = (
                    ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                    .filterBounds(geom).filterDate(ini_m.isoformat(), fin_m.isoformat())
                    .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 70))
                )
                val = col_m.map(_ndvi_s2).median().reduceRegion(
                    ee.Reducer.mean(), geom, scale=10, maxPixels=1e9,
                ).get("NDVI").getInfo()
                if val is not None:
                    serie.append({"fecha": fin_m.isoformat(), "ndvi": round(val, 3)})

            salida["lotes"].append({
                "id": lote["id"], "nombre": lote.get("nombre", lote["id"]),
                "tile": tile, "zonas": zonas, "serie": serie,
                "centro": geom.centroid(1).coordinates().getInfo(),
            })
            print("lote ambientado:", lote["id"], "-", len(zonas), "zonas")
        except Exception as e:
            print("lote omitido:", lote.get("id"), "-", e)

    pathlib.Path("site/lotes_data.json").write_text(json.dumps(salida, indent=2))
    print("lotes_data.json escrito:", len(salida["lotes"]), "lotes")


def main():
    inicializar()

    departamentos = (
        ee.FeatureCollection("FAO/GAUL/2015/level2")
        .filter(ee.Filter.eq("ADM0_NAME", "Argentina"))
    )
    arg = departamentos.geometry()

    if not pathlib.Path("site/departamentos.geojson").exists():
        deptos = departamentos.map(
            lambda f: ee.Feature(f.simplify(1000)).select(["ADM1_NAME", "ADM2_NAME"])
        )
        url_geo = deptos.getDownloadURL(filetype="geojson")
        gj = requests.get(url_geo, timeout=600)
        gj.raise_for_status()
        pathlib.Path("site").mkdir(exist_ok=True)
        pathlib.Path("site/departamentos.geojson").write_bytes(gj.content)
        print("departamentos.geojson generado:", len(gj.content) // 1024, "KB")

    s2 = sentinel2(arg).clip(arg)
    mod = modis(arg).clip(arg)
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
    optico = INICIO + " a " + FIN
    salida["rangos"] = {
        "NDVI_s2": optico,
        "EVI_s2": optico,
        "NDWI_s2": optico,
        "MNDWI_s2": optico,
        "NDVI_modis": (HOY - dt.timedelta(days=80)).isoformat() + " a " + FIN,
        "lluvia_chirps": INICIO_LLUVIA + " a " + FIN,
        "lluvia_era5": INICIO_LLUVIA + " a " + FIN,
    }

    try:
        tiles.update(capas_topografia(arg))
        for k in ["topo_elevacion", "topo_pendiente", "topo_sombreado", "topo_relieve"]:
            salida["rangos"][k] = "capa estatica (Copernicus DEM 30 m)"
        print("topografia: 4 capas listas")
    except Exception as e:
        print("topografia omitida:", e)

    try:
        granizo, url_granizo = reporte_granizo(arg, departamentos)
        tiles["granizo_ayer"] = url_granizo
        salida["granizo"] = granizo
        salida["rangos"]["granizo_ayer"] = granizo["fecha"]
        actualizar_historial("historial_granizo.json", granizo)
        print("granizo:", len(granizo["departamentos"]), "departamentos afectados ayer")
    except Exception as e:
        print("reporte granizo omitido:", e)

    try:
        heladas, url_tmin, url_hr = reporte_heladas(arg, departamentos)
        tiles["tmin_heladas"] = url_tmin
        tiles["hr_madrugada"] = url_hr
        salida["heladas"] = heladas
        salida["rangos"]["tmin_heladas"] = "madrugada del " + heladas["fecha"]
        salida["rangos"]["hr_madrugada"] = "madrugada del " + heladas["fecha"]
        resumen = {
            "fecha": heladas["fecha"],
            "departamentos": [d for d in heladas["departamentos"] if d["tmin_c"] <= 3],
        }
        actualizar_historial("historial_heladas.json", resumen)
        n_helada = sum(1 for d in heladas["departamentos"] if d["tmin_c"] <= 0)
        print("heladas:", n_helada, "departamentos con minima <= 0 C el", heladas["fecha"])
    except Exception as e:
        print("reporte heladas omitido:", e)

    try:
        inundacion, tiles_inund = reporte_inundaciones(arg, departamentos)
        tiles.update(tiles_inund)
        salida["inundacion"] = inundacion
        salida["rangos"]["lluvia_imerg72"] = (HOY - dt.timedelta(days=3)).isoformat() + " a " + FIN
        salida["rangos"]["agua_s1"] = (HOY - dt.timedelta(days=12)).isoformat() + " a " + FIN
        salida["rangos"]["agua_nueva"] = (HOY - dt.timedelta(days=12)).isoformat() + " a " + FIN
        salida["rangos"]["zonas_bajas"] = "capa estatica (HAND, MERIT Hydro)"
        print("inundacion:", len(inundacion["departamentos"]), "departamentos con senal")
    except Exception as e:
        print("reporte inundaciones omitido:", e)

    try:
        media = (
            mod.select("NDVI")
            .reduceRegion(ee.Reducer.mean(), arg, scale=5000, maxPixels=1e13)
            .get("NDVI").getInfo()
        )
        salida["ndvi_medio_nacional"] = round(media, 3) if media else None
    except Exception as e:
        print("ndvi medio omitido:", e)
        salida["ndvi_medio_nacional"] = None

    # Serie historica nacional (para graficos de tendencia)
    g_ = salida.get("granizo") or {}
    h_ = salida.get("heladas") or {}
    inu_ = salida.get("inundacion") or {}
    registro_nacional = {
        "fecha": FIN,
        "ndvi_medio": salida["ndvi_medio_nacional"],
        "deptos_granizo": len(g_.get("departamentos", [])),
        "deptos_helada_bajo0": sum(1 for d in h_.get("departamentos", []) if d.get("tmin_c", 99) <= 0),
        "deptos_inundacion": len(inu_.get("departamentos", [])),
    }
    actualizar_historial("serie_nacional.json", registro_nacional, maximo=120)

    pathlib.Path("site").mkdir(exist_ok=True)
    pathlib.Path("site/tiles.json").write_text(json.dumps(salida, indent=2))
    print("tiles.json escrito. NDVI medio:", salida["ndvi_medio_nacional"])

    try:
        ambientar_lotes()
    except Exception as e:
        print("ambientar_lotes omitido:", e)

    thumb_url = mod.select("NDVI").visualize(**PALETAS["NDVI"]).getThumbURL(
        {"region": arg, "dimensions": 1024, "format": "png"}
    )
    png = requests.get(thumb_url, timeout=300)
    png.raise_for_status()
    pathlib.Path("site/mapa_ndvi.png").write_bytes(png.content)
    print("mapa_ndvi.png descargado:", len(png.content) // 1024, "KB")


if __name__ == "__main__":
    main()
