"""Envia el mapa del dia por WhatsApp usando Twilio."""

import json
import os
import pathlib

from twilio.rest import Client


def main():
    datos = json.loads(pathlib.Path("site/tiles.json").read_text())
    ndvi = datos.get("ndvi_medio_nacional")

    texto = (
        "Monitor Satelital Argentina - " + str(datos["fecha"]) + "\n"
        + "NDVI medio nacional: " + str(ndvi) + "\n"
        + "Compuesto de los ultimos " + str(datos["ventana_dias"]) + " dias.\n"
        + "Dashboard: " + os.environ.get("SITE_URL", "")
    )

    client = Client(os.environ["TWILIO_SID"], os.environ["TWILIO_TOKEN"])
    msg = client.messages.create(
        from_="whatsapp:" + os.environ.get("TWILIO_WHATSAPP_FROM", "+14155238886"),
        to="whatsapp:" + os.environ["WHATSAPP_TO"],
        body=texto,
        media_url=[os.environ["PNG_URL"]],
    )
    print("WhatsApp enviado:", msg.sid)


if __name__ == "__main__":
    main()
