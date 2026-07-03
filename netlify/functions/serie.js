// Funcion Netlify: series temporales de indices desde Google Earth Engine
const ee = require('@google/earthengine');

let inicializado = false;

function initEE() {
  return new Promise(function (resolve, reject) {
    if (inicializado) return resolve();
    const clave = JSON.parse(process.env.GEE_SA_KEY);
    ee.data.authenticateViaPrivateKey(
      clave,
      function () {
        ee.initialize(null, null, function () { inicializado = true; resolve(); }, reject);
      },
      reject
    );
  });
}

function mascaraS2(img) {
  const scl = img.select('SCL');
  const mala = scl.eq(3).or(scl.eq(8)).or(scl.eq(9)).or(scl.eq(10)).or(scl.eq(11));
  return img.updateMask(mala.not());
}

function coleccion(indice, geom, inicio, fin) {
  if (indice === 'RVI') {
    // Radar Sentinel-1: atraviesa nubes, ideal para seguir cosecha
    const s1 = ee.ImageCollection('COPERNICUS/S1_GRD')
      .filterBounds(geom).filterDate(inicio, fin)
      .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VV'))
      .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VH'))
      .filter(ee.Filter.eq('instrumentMode', 'IW'));
    return s1.map(function (img) {
      const vv = ee.Image(10).pow(img.select('VV').divide(10));
      const vh = ee.Image(10).pow(img.select('VH').divide(10));
      const rvi = vh.multiply(4).divide(vv.add(vh)).rename('valor');
      return rvi.copyProperties(img, ['system:time_start']);
    });
  }
  if (indice === 'TEMP') {
    return ee.ImageCollection('ECMWF/ERA5_LAND/DAILY_AGGR')
      .filterBounds(geom).filterDate(inicio, fin)
      .map(function (img) {
        return img.select('temperature_2m').subtract(273.15).rename('valor')
          .copyProperties(img, ['system:time_start']);
      });
  }
  // Indices opticos Sentinel-2: solo pixeles sin nubes (mascara SCL por imagen)
  const bandas = {
    NDVI: ['B8', 'B4'], GNDVI: ['B8', 'B3'], NDWI: ['B8', 'B12'],
    NDMI: ['B8', 'B11'], MNDWI: ['B3', 'B11']
  };
  const s2 = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
    .filterBounds(geom).filterDate(inicio, fin)
    .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 70))
    .map(mascaraS2);
  if (indice === 'EVI') {
    return s2.map(function (img) {
      const o = img.multiply(0.0001);
      const evi = o.expression('2.5*(N-R)/(N+6*R-7.5*B+1)', { N: o.select('B8'), R: o.select('B4'), B: o.select('B2') }).rename('valor');
      return evi.copyProperties(img, ['system:time_start']);
    });
  }
  const par = bandas[indice] || bandas.NDVI;
  return s2.map(function (img) {
    return img.normalizedDifference(par).rename('valor').copyProperties(img, ['system:time_start']);
  });
}

exports.handler = async function (event) {
  const cors = { 'Access-Control-Allow-Origin': '*', 'Content-Type': 'application/json' };
  try {
    if (event.httpMethod === 'OPTIONS') return { statusCode: 200, headers: cors, body: '' };
    const datos = JSON.parse(event.body);
    await initEE();
    const geom = ee.Geometry(datos.geometry);
    const col = ee.ImageCollection(coleccion(datos.indice, geom, datos.inicio, datos.fin));
    const escala = datos.indice === 'TEMP' ? 9000 : 20;
    const fc = ee.FeatureCollection(col.map(function (img) {
      const media = ee.Image(img).reduceRegion({ reducer: ee.Reducer.mean(), geometry: geom, scale: escala, maxPixels: 1e9 }).get('valor');
      return ee.Feature(null, { fecha: ee.Image(img).date().format('YYYY-MM-dd'), valor: media });
    })).filter(ee.Filter.notNull(['valor']));
    const salida = await new Promise(function (resolve, reject) {
      fc.evaluate(function (r, e) { e ? reject(new Error(e)) : resolve(r); });
    });
    const puntos = salida.features.map(function (f) { return f.properties; });
    puntos.sort(function (a, b) { return a.fecha < b.fecha ? -1 : 1; });
    return { statusCode: 200, headers: cors, body: JSON.stringify({ indice: datos.indice, puntos: puntos }) };
  } catch (e) {
    return { statusCode: 500, headers: cors, body: JSON.stringify({ error: String((e && e.message) || e) }) };
  }
};
