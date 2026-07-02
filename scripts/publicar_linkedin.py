"""Publica el mapa del dia en LinkedIn (perfil personal)."""

import json
import os
import pathlib

import requests

API = "https://api.linkedin.com/v2"


def main():
    token = os.environ["LINKEDIN_TOKEN"]
    autor = os.environ["LINKEDIN_PERSON_URN"]
    headers = {
        "Authorization": "Bearer " + token,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }

    datos = json.loads(pathlib.Path("site/tiles.json").read_text())

    registro = requests.post(
        API + "/assets?action=registerUpload",
        headers=headers,
        json={
            "registerUploadRequest": {
                "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                "owner": autor,
                "serviceRelationships": [{
                    "relationshipType": "OWNER",
                    "identifier": "urn:li:userGeneratedContent",
                }],
            }
        },
        timeout=60,
    )
    registro.raise_for_status()
    valor = registro.json()["value"]
    upload_url = valor["uploadMechanism"][
        "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"
    ]["uploadUrl"]
    asset = valor["asset"]

    png = pathlib.Path("site/mapa_ndvi.png").read_bytes()
    subida = requests.put(
        upload_url,
        headers={"Authorization": "Bearer " + token},
        data=png,
        timeout=120,
    )
    subida.raise_for_status()

    texto = (
        "Monitoreo satelital de Argentina - " + str(datos["fecha"]) + "\n\n"
        + "NDVI medio nacional: " + str(datos.get("ndvi_medio_nacional")) + "\n"
        + "Indices de vegetacion y precipitaciones actualizados con Sentinel-2, "
        + "MODIS, CHIRPS y ERA5 via Google Earth Engine.\n\n"
        + "Dashboard interactivo: " + os.environ.get("SITE_URL", "") + "\n\n"
        + "#Teledeteccion #GEE #Agro #Argentina"
    )
    post = requests.post(
        API + "/ugcPosts",
        headers=headers,
        json={
            "author": autor,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": texto},
                    "shareMediaCategory": "IMAGE",
                    "media": [{"status": "READY", "media": asset}],
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        },
        timeout=60,
    )
    post.raise_for_status()
    print("Publicado en LinkedIn:", post.headers.get("x-restli-id"))


if __name__ == "__main__":
    main()
