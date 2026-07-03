"""Envia el mapa del dia por WhatsApp usando Twilio, con avisos de
helada, granizo e inundacion si los hay."""

import json
import os
import pathlib

from twilio.rest import Client


def main():
    datos = json.loads(pathlib.Path("site/tiles.json").read_text())
    ndvi = datos.get("ndvi_medio_nacional")

    lineas = [
        "Monitor Satelital Argentina - " + str(datos["fecha"]),
        "NDVI medio nacional: " + str(ndvi),
    ]

    heladas = datos.get("heladas")
    if heladas:
        frios = [d for d in heladas["departamentos"] if d["tmin_c"] <= 0]
        riesgo = [d for d in heladas["departamentos"] if 0 < d["tmin_c"] <= 3]
        if frios:
            peor = frios[0]
            lineas.append(
                "HELADA (madrugada del " + heladas["fecha"] + "): "
                + str(len(frios)) + " deptos bajo 0 C. Minima: "
                + str(peor["tmin_c"]) + " C en " + peor["departamento"]
                + " (" + peor["provincia"] + ")."
            )
        elif riesgo:
            lineas.append(
                "Riesgo de helada (madrugada del " + heladas["fecha"] + "): "
                + str(len(riesgo)) + " deptos entre 0 y 3 C."
            )

    granizo = datos.get("granizo")
    if granizo and granizo["departamentos"]:
        peor = granizo["departamentos"][0]
        lineas.append(
            "POSIBLE GRANIZO ayer (" + granizo["fecha"] + "): "
            + str(len(granizo["departamentos"])) + " deptos con conveccion severa. "
            + "Nucleo mas frio: " + peor["departamento"] + " (" + peor["provincia"] + ")."
        )

    inund = datos.get("inundacion")
    if inund and inund["departamentos"]:
        peor = inund["departamentos"][0]
        lineas.append(
            "AGUA/INUNDACION: " + str(len(inund["departamentos"]))
            + " deptos con senal. Mayor superficie: " + peor["departamento"]
            + " (" + peor["provincia"] + "), " + str(peor["agua_nueva_ha"]) + " ha."
        )

    if len(lineas) == 2:
        lineas.append("Sin alertas de helada, granizo ni inundacion.")

    lineas.append("Dashboard: " + os.environ.get("SITE_URL", ""))
    texto = "\n".join(lineas)

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
