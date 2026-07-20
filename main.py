import json
import logging
import os
import unicodedata

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

# ---------------------------------------------------------
# Configuración de logs
# ---------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("fami-api")

# ---------------------------------------------------------
# Aplicación
# ---------------------------------------------------------

app = FastAPI(title="Fami API")

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "fami_catalogo_2026_seguro").strip()
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "EAAOebZAD1zV4BSGzmO540JNLOG4hO5uphkk2razPuyzCdKElc1bRhNwZAqnkOVeQqXbt8v1rfveDZCUK37zaJuQ1sfTURTLMAoCheeIIZBuX2oGzB5ZAEJrGLOeOL7mhk6jU2ordfxlzWBXGE3jzoHWuVDjcZAZBPGbCvplQZA1XbB6RSkOumeI5ZBf47uXzIwnQFaD75aCoXJS55YQCCDKN6vhMsyjQ7qqZBwX7ufQYVhFWyAh3HaaTNHF39qZCZA2P5TSKB0bbqCXLarUBj6JDLGHcOs86").strip()
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "1229341023598915").strip()
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v23.0").strip()

CATALOGO_URL = "https://nilsonrc9.wixsite.com/famalandia/tienda"


@app.on_event("startup")
async def verificar_configuracion():
    logger.info("Iniciando Fami API")

    logger.info(
        "VERIFY_TOKEN configurado: %s",
        bool(VERIFY_TOKEN),
    )
    logger.info(
        "META_ACCESS_TOKEN configurado: %s",
        bool(META_ACCESS_TOKEN),
    )
    logger.info(
        "PHONE_NUMBER_ID configurado: %s",
        bool(PHONE_NUMBER_ID),
    )
    logger.info(
        "GRAPH_API_VERSION: %s",
        GRAPH_API_VERSION,
    )

    variables_faltantes = []

    if not VERIFY_TOKEN:
        variables_faltantes.append("VERIFY_TOKEN")

    if not META_ACCESS_TOKEN:
        variables_faltantes.append("META_ACCESS_TOKEN")

    if not PHONE_NUMBER_ID:
        variables_faltantes.append("PHONE_NUMBER_ID")

    if variables_faltantes:
        logger.error(
            "Faltan variables de ambiente: %s",
            ", ".join(variables_faltantes),
        )


@app.get("/")
def root():
    return {
        "status": "Fami API funcionando",
        "webhook": "/webhook",
        "configuracion": {
            "verify_token": bool(VERIFY_TOKEN),
            "meta_access_token": bool(META_ACCESS_TOKEN),
            "phone_number_id": bool(PHONE_NUMBER_ID),
            "graph_api_version": GRAPH_API_VERSION,
        },
    }


# ---------------------------------------------------------
# Verificación del webhook
# ---------------------------------------------------------

@app.get("/webhook", response_class=PlainTextResponse)
def verify_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(
        default=None,
        alias="hub.verify_token",
    ),
    hub_challenge: str | None = Query(
        default=None,
        alias="hub.challenge",
    ),
):
    logger.info(
        "Solicitud de verificación recibida. Modo: %s",
        hub_mode,
    )

    if (
        hub_mode == "subscribe"
        and hub_verify_token == VERIFY_TOKEN
        and hub_challenge is not None
    ):
        logger.info("Webhook verificado correctamente")
        return hub_challenge

    logger.warning("Meta intentó verificar con un token incorrecto")

    raise HTTPException(
        status_code=403,
        detail="Token incorrecto",
    )


# ---------------------------------------------------------
# Utilidades
# ---------------------------------------------------------

def normalizar_texto(texto: str) -> str:
    texto = texto.lower().strip()

    return "".join(
        caracter
        for caracter in unicodedata.normalize("NFD", texto)
        if unicodedata.category(caracter) != "Mn"
    )


async def enviar_mensaje(
    numero_destino: str,
    mensaje: str,
) -> bool:
    if not META_ACCESS_TOKEN:
        logger.error("No existe META_ACCESS_TOKEN en Render")
        return False

    if not PHONE_NUMBER_ID:
        logger.error("No existe PHONE_NUMBER_ID en Render")
        return False

    url = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
        f"{PHONE_NUMBER_ID}/messages"
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

    logger.info(
        "Intentando enviar respuesta al número terminado en %s",
        numero_destino[-4:],
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            respuesta = await client.post(
                url,
                headers=headers,
                json=payload,
            )

        logger.info(
            "Respuesta de Meta: HTTP %s",
            respuesta.status_code,
        )

        if respuesta.status_code >= 400:
            logger.error(
                "Meta rechazó el mensaje: %s",
                respuesta.text,
            )
            return False

        logger.info(
            "Mensaje enviado correctamente: %s",
            respuesta.text,
        )

        return True

    except httpx.TimeoutException:
        logger.exception(
            "La conexión con Meta excedió el tiempo de espera"
        )
        return False

    except httpx.HTTPError:
        logger.exception(
            "Error de conexión al comunicarse con Meta"
        )
        return False


async def procesar_mensaje(mensaje: dict) -> None:
    tipo_mensaje = mensaje.get("type")
    numero_cliente = mensaje.get("from", "")

    logger.info(
        "Mensaje recibido. Tipo: %s | Remitente terminado en: %s",
        tipo_mensaje,
        numero_cliente[-4:] if numero_cliente else "desconocido",
    )

    if tipo_mensaje != "text":
        logger.info("Mensaje ignorado porque no es texto")
        return

    texto = mensaje.get("text", {}).get("body", "")
    texto_normalizado = normalizar_texto(texto)

    logger.info(
        "Texto recibido y normalizado: %s",
        texto_normalizado,
    )

    palabras_catalogo = [
        "catalogo",
        "quiero el catalogo",
        "enviame el catalogo",
        "ver catalogo",
        "ver productos",
        "quiero ver sus productos",
    ]

    solicita_catalogo = any(
        frase in texto_normalizado
        for frase in palabras_catalogo
    )

    if not solicita_catalogo:
        logger.info(
            "El mensaje no coincide con la solicitud de catálogo"
        )
        return

    respuesta_cliente = (
        "🛍️ ¡Claro! Puedes revisar nuestro catálogo aquí:\n\n"
        f"{CATALOGO_URL}\n\n"
        "✨ Encuentra lo inesperado."
    )

    enviado = await enviar_mensaje(
        numero_cliente,
        respuesta_cliente,
    )

    if enviado:
        logger.info(
            "Respuesta automática completada correctamente"
        )
    else:
        logger.error(
            "La solicitud llegó, pero no se pudo responder"
        )


# ---------------------------------------------------------
# Recepción de eventos
# ---------------------------------------------------------

@app.post("/webhook")
async def recibir_webhook(request: Request):
    try:
        datos = await request.json()

    except Exception:
        logger.exception("Meta envió un cuerpo que no es JSON")
        raise HTTPException(
            status_code=400,
            detail="JSON inválido",
        )

    logger.info(
        "Webhook recibido:\n%s",
        json.dumps(
            datos,
            ensure_ascii=False,
            indent=2,
        ),
    )

    mensajes_encontrados = 0

    try:
        for entrada in datos.get("entry", []):
            for cambio in entrada.get("changes", []):
                valor = cambio.get("value", {})
                mensajes = valor.get("messages", [])

                for mensaje in mensajes:
                    mensajes_encontrados += 1
                    await procesar_mensaje(mensaje)

        if mensajes_encontrados == 0:
            logger.info(
                "Evento recibido sin mensajes. "
                "Probablemente sea un estado de entrega o lectura."
            )

        return {
            "status": "recibido",
            "mensajes_procesados": mensajes_encontrados,
        }

    except Exception:
        logger.exception(
            "Ocurrió un error procesando el webhook"
        )

        # Se responde 200 para evitar que Meta repita el mismo evento
        return {
            "status": "error procesando evento",
        }
