from __future__ import annotations

import hmac
import logging
import os
import unicodedata
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from database import (
    DATABASE_URL, get_conversation, get_operator, normalize_phone,
    save_audit, save_incoming_text, save_outgoing_text,
    update_message_status,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("fami-api")
app = FastAPI(title="Fami API")

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "").strip()
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "").strip()
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "").strip()
INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN", "").strip()
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v23.0").strip()
CATALOGO_URL = "https://nilsonrc9.wixsite.com/famalandia/tienda"


class SendTextRequest(BaseModel):
    phone_number: str
    conversation_id: int
    text: str = Field(min_length=1, max_length=4096)
    user_id: str


def normalizar_texto(texto: str) -> str:
    lowered = str(texto or "").lower().strip()
    return "".join(
        char for char in unicodedata.normalize("NFD", lowered)
        if unicodedata.category(char) != "Mn"
    )


def solicita_catalogo(texto: str) -> bool:
    normalized = normalizar_texto(texto)
    return any(
        phrase in normalized
        for phrase in (
            "catalogo", "quiero el catalogo", "enviame el catalogo",
            "ver catalogo", "ver productos", "quiero ver sus productos",
        )
    )


def _message_id(response: dict[str, Any]) -> str:
    messages = response.get("messages") or []
    if not messages or not messages[0].get("id"):
        raise RuntimeError("Meta no devolvió el identificador del mensaje.")
    return str(messages[0]["id"])


async def enviar_mensaje(numero_destino: str, mensaje: str) -> dict[str, Any]:
    if not META_ACCESS_TOKEN or not PHONE_NUMBER_ID:
        raise RuntimeError("La conexión con Meta no está configurada.")
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalize_phone(numero_destino).lstrip("+"),
        "type": "text",
        "text": {"preview_url": True, "body": mensaje},
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {META_ACCESS_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        response.raise_for_status()
        result = response.json()
        _message_id(result)
        logger.info("Mensaje aceptado por Meta para destino terminado en %s", numero_destino[-4:])
        return result
    except httpx.HTTPStatusError as error:
        logger.error("Meta rechazó el mensaje: HTTP %s", error.response.status_code)
        raise RuntimeError("Meta rechazó el mensaje.") from error
    except httpx.HTTPError as error:
        logger.exception("Error de conexión al comunicarse con Meta")
        raise RuntimeError("No fue posible conectar con Meta.") from error


async def _process_incoming_message(
    message: dict[str, Any], contacts: dict[str, str],
) -> None:
    message_type = str(message.get("type") or "other")
    message_id = str(message.get("id") or "")
    phone = str(message.get("from") or "")
    logger.info(
        "Mensaje entrante tipo=%s id=%s remitente=***%s",
        message_type, message_id, phone[-4:] if phone else "",
    )
    if message_type != "text" or not message_id or not phone:
        logger.info("Tipo de mensaje reservado para una fase posterior")
        return
    body = str((message.get("text") or {}).get("body") or "")
    if not DATABASE_URL:
        logger.warning("Persistencia WhatsApp pendiente: DATABASE_URL no configurada")
        if solicita_catalogo(body):
            automatic_text = (
                "🛒 ¡Claro! Puedes revisar nuestro catálogo aquí:\n\n"
                f"{CATALOGO_URL}\n\n✨ Encuentra lo inesperado."
            )
            await enviar_mensaje(phone, automatic_text)
        return
    conversation_id, inserted = save_incoming_text(
        whatsapp_message_id=message_id,
        phone=phone,
        wa_id=phone,
        display_name=contacts.get(phone),
        text_body=body,
        whatsapp_timestamp=message.get("timestamp"),
        raw_payload=message,
    )
    if not inserted:
        logger.info("Mensaje duplicado ignorado id=%s", message_id)
        return
    if solicita_catalogo(body):
        automatic_text = (
            "🛒 ¡Claro! Puedes revisar nuestro catálogo aquí:\n\n"
            f"{CATALOGO_URL}\n\n✨ Encuentra lo inesperado."
        )
        result = await enviar_mensaje(phone, automatic_text)
        save_outgoing_text(
            whatsapp_message_id=_message_id(result),
            conversation_id=conversation_id,
            phone=phone,
            text_body=automatic_text,
            sent_by_user_id=None,
            sent_by_user_name="Automatización de catálogo",
            automated=True,
            raw_payload=result,
        )


async def process_webhook_payload(payload: dict[str, Any]) -> None:
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value") or {}
                contacts = {
                    str(contact.get("wa_id") or ""): str(
                        (contact.get("profile") or {}).get("name") or ""
                    )
                    for contact in value.get("contacts", [])
                }
                for status in value.get("statuses", []):
                    message_id = str(status.get("id") or "")
                    state = str(status.get("status") or "")
                    if (
                        DATABASE_URL
                        and message_id
                        and update_message_status(message_id, state)
                    ):
                        logger.info("Estado actualizado id=%s estado=%s", message_id, state)
                for message in value.get("messages", []):
                    await _process_incoming_message(message, contacts)
    except Exception:
        logger.exception("Error procesando evento de WhatsApp en segundo plano")


@app.on_event("startup")
async def verify_configuration() -> None:
    configured = {
        "VERIFY_TOKEN": bool(VERIFY_TOKEN),
        "META_ACCESS_TOKEN": bool(META_ACCESS_TOKEN),
        "PHONE_NUMBER_ID": bool(PHONE_NUMBER_ID),
        "DATABASE_URL": bool(DATABASE_URL),
        "INTERNAL_API_TOKEN": bool(INTERNAL_API_TOKEN),
    }
    logger.info("Iniciando Fami API Graph=%s config=%s", GRAPH_API_VERSION, configured)


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "status": "Fami API funcionando",
        "webhook": "/webhook",
        "configuracion": {
            "verify_token": bool(VERIFY_TOKEN),
            "meta_access_token": bool(META_ACCESS_TOKEN),
            "phone_number_id": bool(PHONE_NUMBER_ID),
            "database": bool(DATABASE_URL),
            "internal_api_token": bool(INTERNAL_API_TOKEN),
            "graph_api_version": GRAPH_API_VERSION,
        },
    }


@app.get("/webhook", response_class=PlainTextResponse)
def verify_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> str:
    if (
        hub_mode == "subscribe" and VERIFY_TOKEN
        and hmac.compare_digest(hub_verify_token or "", VERIFY_TOKEN)
        and hub_challenge is not None
    ):
        return hub_challenge
    raise HTTPException(status_code=403, detail="Token incorrecto")


@app.post("/webhook")
async def receive_webhook(
    request: Request, background_tasks: BackgroundTasks,
) -> dict[str, str]:
    try:
        payload = await request.json()
    except Exception as error:
        raise HTTPException(status_code=400, detail="JSON inválido") from error
    event_count = sum(
        len((change.get("value") or {}).get("messages", []))
        + len((change.get("value") or {}).get("statuses", []))
        for entry in payload.get("entry", [])
        for change in entry.get("changes", [])
    )
    logger.info("Webhook aceptado con %s eventos procesables", event_count)
    background_tasks.add_task(process_webhook_payload, payload)
    return {"status": "recibido"}


@app.post("/internal/whatsapp/send-text")
async def send_internal_text(
    data: SendTextRequest,
    x_internal_token: str | None = Header(default=None),
) -> dict[str, Any]:
    if not INTERNAL_API_TOKEN or not hmac.compare_digest(
        x_internal_token or "", INTERNAL_API_TOKEN
    ):
        raise HTTPException(status_code=401, detail="Acceso interno no autorizado")
    operator = get_operator(data.user_id)
    if not operator or str(operator["rol"]) not in {"ADMINISTRADOR", "REGISTRO"}:
        raise HTTPException(status_code=403, detail="Operador no autorizado")
    conversation = get_conversation(data.conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")
    try:
        requested_phone = normalize_phone(data.phone_number)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if requested_phone != normalize_phone(str(conversation["phone_number"])):
        raise HTTPException(status_code=409, detail="El número no corresponde a la conversación")
    try:
        result = await enviar_mensaje(requested_phone, data.text.strip())
        message_id = _message_id(result)
        save_outgoing_text(
            whatsapp_message_id=message_id,
            conversation_id=data.conversation_id,
            phone=requested_phone,
            text_body=data.text.strip(),
            sent_by_user_id=str(operator["id"]),
            sent_by_user_name=str(operator["nombre_completo"]),
            automated=False,
            raw_payload=result,
        )
        save_audit(
            user_id=str(operator["id"]),
            conversation_id=data.conversation_id,
            action="whatsapp_message_sent",
            new_data={"whatsapp_message_id": message_id},
        )
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return {
        "ok": True,
        "message_id": message_id,
        "sent_by_user_id": str(operator["id"]),
        "sent_by_user_name": str(operator["nombre_completo"]),
    }
