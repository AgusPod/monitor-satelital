// Funcion Netlify: ultima imagen GOES-19 (tiempo casi real, cada 10 min)
// Devuelve tiles de nubes (tope frio) y conveccion severa (posible granizo)
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

function mapid(imagen, vis) {
  return new Promise(function (resolve, reject) {
    imagen.getMapId(vis, function (m, e) { e ? reject(new Error(e)) : resolve(m.urlFormat); });
  });
}

exports.handler = async function (event) {
  const cors = { 'Access-Control-Allow-Origin': '*', 'Content-Type': 'application/json' };
  try {
    await initEE();
    const ahora = Date.now();
    const desde = new Date(ahora - 3 * 3600 * 1000).toISOString();
    const hasta = new Date(ahora + 600000).toISOString();
    const img = ee.Image(
      ee.ImageCollection('NOAA/GOES/19/MCMIPF')
        .filterDate(desde, hasta)
        .sort('system:time_start', false)
        .first()
    );
    // La banda C13 (10.3 um) trae temperatura de brillo en K, con escala y offset en propiedades
    const escala = ee.Number(img.get('CMI_C13_scale'));
    const offset = ee.Number(img.get('CMI_C13_offset'));
    const c13 = img.select('CMI_C13').multiply(escala).add(offset);
    // Nubes: todo tope mas frio que ~10 C (283 K); blanco = mas frio/alto
    const nubes = c13.updateMask(c13.lt(283));
    // Conveccion severa: topes < 220 K (-53 C), tormentas profundas con riesgo de granizo
    const severo = c13.updateMask(c13.lt(220));
    const visNubes = { min: 190, max: 283, palette: ['ffffff', 'e8e8e8', '9aa4af', '333a44'] };
    const visSevero = { min: 185, max: 220, palette: ['ff00ff', 'ff0000', 'ffa500', 'ffff00'] };
    const fecha = await new Promise(function (resolve, reject) {
      img.date().format('YYYY-MM-dd HH:mm').evaluate(function (r, e) { e ? reject(new Error(e)) : resolve(r); });
    });
    const urlNubes = await mapid(nubes, visNubes);
    const urlSevero = await mapid(severo, visSevero);
    return {
      statusCode: 200, headers: cors,
      body: JSON.stringify({ fecha_imagen: fecha + ' UTC', tiles: { nubes: urlNubes, severo: urlSevero } })
    };
  } catch (e) {
    return { statusCode: 500, headers: cors, body: JSON.stringify({ error: String((e && e.message) || e) }) };
  }
};
