import os
import unicodedata

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

app = FastAPI()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v23.0")

CATALOGO_URL = "https://nilsonrc9.wixsite.com/famalandia/tienda"


@app.get("/")
def root():
    return {"status": "Fami API funcionando"}


@app.get("/webhook", response_class=PlainTextResponse)
def verify_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    if (
        hub_mode == "subscribe"
        and hub_verify_token == VERIFY_TOKEN
        and hub_challenge is not None
    ):
        return hub_challenge

    raise HTTPException(status_code=403, detail="Token incorrecto")


def normalizar_texto(texto: str) -> str:
    texto = texto.lower().strip()
    return "".join(
        caracter
        for caracter in unicodedata.normalize("NFD", texto)
        if unicodedata.category(caracter) != "Mn"
    )


async def enviar_mensaje(numero_destino: str, mensaje: str) -> None:
    url = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    )

    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero_destino,
        "type": "text",
        "text": {
            "preview_url": True,
            "body": mensaje,
        },
    }

    async with httpx.AsyncClient(timeout=20) as client:
        respuesta = await client.post(url, headers=headers, json=payload)

    if respuesta.status_code >= 400:
        print(
            "Error al enviar mensaje:",
            respuesta.status_code,
            respuesta.text,
        )


@app.post("/webhook")
async def recibir_webhook(request: Request):
    datos = await request.json()

    try:
        cambio = datos["entry"][0]["changes"][0]["value"]
        mensajes = cambio.get("messages", [])

        if not mensajes:
            return {"status": "evento ignorado"}

        mensaje = mensajes[0]
        numero_cliente = mensaje["from"]

        if mensaje.get("type") != "text":
            return {"status": "mensaje no textual ignorado"}

        texto = mensaje["text"]["body"]
        texto_normalizado = normalizar_texto(texto)

        palabras_catalogo = [
            "catalogo",
            "quiero el catalogo",
            "enviame el catalogo",
            "ver catalogo",
            "ver productos",
            "quiero ver sus productos",
        ]

        if any(frase in texto_normalizado for frase in palabras_catalogo):
            respuesta = (
                "🛍️ ¡Claro! Puedes revisar nuestro catálogo aquí:\n\n"
                f"{CATALOGO_URL}\n\n"
                "✨ Encuentra lo inesperado."
            )

            await enviar_mensaje(numero_cliente, respuesta)

        return {"status": "recibido"}

    except (KeyError, IndexError, TypeError) as error:
        print("Evento no reconocido:", error, datos)
        return {"status": "evento no reconocido"}
