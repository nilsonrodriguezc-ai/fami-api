from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
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
META_BUSINESS_ID = (
    os.getenv("META_BUSINESS_ID") or os.getenv("BUSINESS_ID") or ""
).strip()
KNOWN_DIAGNOSTIC_WABA_ID = "1553678296345682"
RESPOND_IO_APP_ID = "1595811571129902"
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


def _redact_secrets(value: Any) -> str:
    """Evita que una respuesta externa exponga secretos en los logs."""
    sanitized = str(value)
    for secret in (META_ACCESS_TOKEN, INTERNAL_API_TOKEN, DATABASE_URL):
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    sanitized = re.sub(
        r"(?i)([\"']?(?:access_token|authorization|password|service_role|"
        r"database_url|internal_api_token|meta_access_token)[\"']?\s*[:=]\s*)"
        r"(?:[\"'][^\"']*[\"']|[^,}\s]+)",
        r"\1[REDACTED]",
        sanitized,
    )
    return sanitized


def _sanitize_diagnostic_value(value: Any) -> Any:
    sensitive_names = {
        "access_token", "authorization", "password", "service_role",
        "database_url", "internal_api_token", "meta_access_token",
    }
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if str(key).strip().casefold() in sensitive_names
                else _sanitize_diagnostic_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_diagnostic_value(item) for item in value]
    if isinstance(value, str):
        return _redact_secrets(value)
    return value


def _safe_meta_error(
    status_code: int, payload: Any = None,
) -> dict[str, Any]:
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        error = {}
    return {
        "ok": False,
        "http_status": int(status_code),
        "error": {
            "message": _sanitize_diagnostic_value(error.get("message")),
            "type": _sanitize_diagnostic_value(error.get("type")),
            "code": _sanitize_diagnostic_value(error.get("code")),
            "error_subcode": _sanitize_diagnostic_value(
                error.get("error_subcode")
            ),
            "error_data": _sanitize_diagnostic_value(error.get("error_data")),
            "fbtrace_id": _sanitize_diagnostic_value(error.get("fbtrace_id")),
        },
    }


def _decode_json_body(body: bytes) -> dict[str, Any] | None:
    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return result if isinstance(result, dict) else None


def _safe_diagnostic_url(value: Any) -> str | None:
    text_value = str(value or "").strip()
    if not text_value:
        return None
    parsed = urllib.parse.urlsplit(text_value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "", "")
    )


def _debug_current_meta_token_sync() -> dict[str, Any]:
    """Consulta debug_token sin permitir que el token aparezca en logs HTTP."""
    query = urllib.parse.urlencode({"input_token": META_ACCESS_TOKEN})
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/debug_token?{query}"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {META_ACCESS_TOKEN}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status_code = int(response.status)
            payload = _decode_json_body(response.read())
    except urllib.error.HTTPError as error:
        status_code = int(error.code)
        payload = _decode_json_body(error.read())
    except (urllib.error.URLError, TimeoutError):
        return {
            "ok": False,
            "http_status": None,
            "error": {
                "message": "No fue posible conectar con Meta.",
                "type": "ConnectionError",
                "code": None,
                "error_subcode": None,
                "error_data": None,
                "fbtrace_id": None,
            },
        }
    if status_code < 200 or status_code >= 300:
        return _safe_meta_error(status_code, payload)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return _safe_meta_error(status_code, payload)
    allowed = {
        key: data.get(key)
        for key in (
            "is_valid", "app_id", "user_id", "system_user_id", "expires_at",
            "data_access_expires_at", "scopes", "granular_scopes", "type",
        )
        if key in data
    }
    return {
        "ok": True,
        "http_status": status_code,
        "data": _sanitize_diagnostic_value(allowed),
    }


async def _meta_graph_get(
    object_path: str, *, fields: str | None = None,
) -> dict[str, Any]:
    path = str(object_path or "").strip().lstrip("/")
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{path}"
    params = {"fields": fields} if fields else None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {META_ACCESS_TOKEN}"},
                params=params,
            )
    except httpx.HTTPError:
        return {
            "ok": False,
            "http_status": None,
            "error": {
                "message": "No fue posible conectar con Meta.",
                "type": "ConnectionError",
                "code": None,
                "error_subcode": None,
                "error_data": None,
                "fbtrace_id": None,
            },
        }
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        payload = None
    if not response.is_success:
        return _safe_meta_error(response.status_code, payload)
    return {
        "ok": True,
        "http_status": response.status_code,
        "data": _sanitize_diagnostic_value(payload or {}),
    }


def _granular_waba_candidates(
    token_check: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    token_data = token_check.get("data") if token_check.get("ok") else None
    granular_scopes = (
        token_data.get("granular_scopes")
        if isinstance(token_data, dict)
        else None
    )
    if isinstance(granular_scopes, list):
        for scope in granular_scopes:
            if not isinstance(scope, dict):
                continue
            scope_name = str(scope.get("scope") or "")
            if "whatsapp" not in scope_name.casefold():
                continue
            for target_id in scope.get("target_ids") or []:
                candidates.append({
                    "id": str(target_id),
                    "name": None,
                    "source": f"debug_token.granular_scopes.{scope_name}",
                    "business_id": None,
                })
    return candidates


def _unique_waba_candidates(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate_id = str(candidate.get("id") or "")
        if (
            candidate_id
            and candidate_id != PHONE_NUMBER_ID
            and candidate_id not in seen
        ):
            seen.add(candidate_id)
            unique.append(candidate)
    return unique[:50]


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
        response = error.response
        logger.error("Meta rechazó el mensaje")
        logger.error("Meta request URL: %s", url)
        logger.error("Meta HTTP status: %s", response.status_code)
        logger.error("Meta response.text: %s", _redact_secrets(response.text))
        try:
            response_data = response.json()
        except (ValueError, json.JSONDecodeError):
            response_data = None
        meta_error = (
            response_data.get("error")
            if isinstance(response_data, dict)
            else None
        )
        if isinstance(meta_error, dict):
            logger.error(
                "Meta error.message: %s",
                _redact_secrets(meta_error.get("message")),
            )
            logger.error(
                "Meta error.type: %s",
                _redact_secrets(meta_error.get("type")),
            )
            logger.error(
                "Meta error.code: %s",
                _redact_secrets(meta_error.get("code")),
            )
            logger.error(
                "Meta error.error_subcode: %s",
                _redact_secrets(meta_error.get("error_subcode")),
            )
            if "error_data" in meta_error:
                logger.error(
                    "Meta error.error_data: %s",
                    _redact_secrets(
                        json.dumps(
                            meta_error.get("error_data"),
                            ensure_ascii=False,
                            default=str,
                        )
                    ),
                )
            logger.error(
                "Meta error.fbtrace_id: %s",
                _redact_secrets(meta_error.get("fbtrace_id")),
            )
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


@app.get("/internal/meta/diagnostics")
async def meta_diagnostics(
    x_internal_token: str | None = Header(default=None),
) -> dict[str, Any]:
    if not INTERNAL_API_TOKEN or not hmac.compare_digest(
        x_internal_token or "", INTERNAL_API_TOKEN
    ):
        raise HTTPException(status_code=401, detail="Acceso interno no autorizado")
    if not META_ACCESS_TOKEN or not PHONE_NUMBER_ID:
        raise HTTPException(
            status_code=503,
            detail="La conexión con Meta no está configurada.",
        )

    token_check = await asyncio.to_thread(_debug_current_meta_token_sync)
    permissions_raw = await _meta_graph_get("me/permissions")
    if permissions_raw.get("ok"):
        permissions_data = permissions_raw.get("data") or {}
        permissions = {
            "ok": True,
            "http_status": permissions_raw["http_status"],
            "data": [
                {
                    "permission": item.get("permission"),
                    "status": item.get("status"),
                }
                for item in permissions_data.get("data") or []
                if isinstance(item, dict)
            ],
        }
    else:
        permissions = permissions_raw

    phone_raw = await _meta_graph_get(
        PHONE_NUMBER_ID,
        fields="id,display_phone_number,verified_name,quality_rating",
    )
    if phone_raw.get("ok"):
        phone_data = phone_raw.get("data") or {}
        phone_check: dict[str, Any] = {
            "ok": True,
            "http_status": phone_raw["http_status"],
            "data": {
                key: phone_data.get(key)
                for key in (
                    "id", "display_phone_number", "verified_name",
                    "quality_rating",
                )
                if key in phone_data
            },
        }
    else:
        phone_check = phone_raw

    account_mode_check = await _meta_graph_get(
        PHONE_NUMBER_ID, fields="account_mode"
    )
    if account_mode_check.get("ok"):
        account_mode_data = account_mode_check.get("data") or {}
        if phone_check.get("ok") and "account_mode" in account_mode_data:
            phone_check["data"]["account_mode"] = account_mode_data[
                "account_mode"
            ]
    else:
        phone_check["account_mode_check"] = account_mode_check

    known_waba_raw = await _meta_graph_get(
        KNOWN_DIAGNOSTIC_WABA_ID, fields="id,name"
    )
    known_numbers_raw = await _meta_graph_get(
        f"{KNOWN_DIAGNOSTIC_WABA_ID}/phone_numbers",
        fields="id,display_phone_number,verified_name,quality_rating",
    )
    known_numbers = (
        (known_numbers_raw.get("data") or {}).get("data") or []
        if known_numbers_raw.get("ok")
        else []
    )
    known_contains_phone = any(
        isinstance(item, dict)
        and str(item.get("id") or "") == PHONE_NUMBER_ID
        for item in known_numbers
    )
    known_waba_data = (
        known_waba_raw.get("data") or {}
        if known_waba_raw.get("ok")
        else {}
    )
    known_waba_errors: dict[str, Any] = {}
    if not known_waba_raw.get("ok"):
        known_waba_errors["waba"] = known_waba_raw.get("error")
    if not known_numbers_raw.get("ok"):
        known_waba_errors["phone_numbers"] = known_numbers_raw.get("error")
    known_waba_test = {
        "waba_id": KNOWN_DIAGNOSTIC_WABA_ID,
        "waba_name": known_waba_data.get("name"),
        "waba_readable": bool(known_waba_raw.get("ok")),
        "phone_numbers_readable": bool(known_numbers_raw.get("ok")),
        "contains_expected_phone_number": known_contains_phone,
        "expected_phone_number_id": PHONE_NUMBER_ID,
        "http_status": {
            "waba": known_waba_raw.get("http_status"),
            "phone_numbers": known_numbers_raw.get("http_status"),
        },
        "phone_numbers": [
            {
                key: item.get(key)
                for key in (
                    "id", "display_phone_number", "verified_name",
                    "quality_rating",
                )
                if key in item
            }
            for item in known_numbers
            if isinstance(item, dict)
        ],
        "errors": known_waba_errors,
    }

    subscriptions_raw = await _meta_graph_get(
        f"{KNOWN_DIAGNOSTIC_WABA_ID}/subscribed_apps"
    )
    subscription_rows = (
        (subscriptions_raw.get("data") or {}).get("data") or []
        if subscriptions_raw.get("ok")
        else []
    )
    subscribed_apps: list[dict[str, Any]] = []
    for item in subscription_rows:
        if not isinstance(item, dict):
            continue
        app_data = item.get("whatsapp_business_api_data") or {}
        if not isinstance(app_data, dict):
            app_data = {}
        subscribed_apps.append({
            "id": str(app_data.get("id") or "") or None,
            "name": app_data.get("name"),
            "link": _safe_diagnostic_url(app_data.get("link")),
            "override_callback_uri": _safe_diagnostic_url(
                item.get("override_callback_uri")
            ),
        })
    token_data_for_apps = (
        token_check.get("data") if token_check.get("ok") else {}
    )
    fami_app_id = str(
        (token_data_for_apps or {}).get("app_id") or ""
    ) or None
    subscribed_ids = {
        str(item.get("id")) for item in subscribed_apps if item.get("id")
    }
    apps_with_override = [
        item for item in subscribed_apps if item.get("override_callback_uri")
    ]
    if len(subscribed_apps) == 1:
        webhook_determination = {
            "determined": True,
            "app": subscribed_apps[0],
            "reason": "Meta devolvió una sola aplicación suscrita a la WABA.",
        }
    elif len(apps_with_override) == 1:
        webhook_determination = {
            "determined": True,
            "app": apps_with_override[0],
            "reason": (
                "Meta devolvió una sola aplicación con callback sobrescrito."
            ),
        }
    else:
        webhook_determination = {
            "determined": False,
            "app": None,
            "reason": (
                "La lista de suscripciones no identifica el callback por "
                "defecto de cada app; con cero o varias apps no puede "
                "atribuirse un único webhook productivo."
            ),
        }
    subscribed_apps_test: dict[str, Any] = {
        "waba_id": KNOWN_DIAGNOSTIC_WABA_ID,
        "ok": bool(subscriptions_raw.get("ok")),
        "http_status": subscriptions_raw.get("http_status"),
        "subscribed_app_count": len(subscribed_apps),
        "more_than_one_app": len(subscribed_apps) > 1,
        "fami_app_id": fami_app_id,
        "fami_subscribed": bool(fami_app_id and fami_app_id in subscribed_ids),
        "respond_io_app_id": RESPOND_IO_APP_ID,
        "respond_io_subscribed": RESPOND_IO_APP_ID in subscribed_ids,
        "apps": subscribed_apps,
        "productive_webhook_app": webhook_determination,
        "sending_permission_note": (
            "La suscripción controla webhooks entrantes y no concede por sí "
            "sola permiso para enviar mensajes."
        ),
    }
    if not subscriptions_raw.get("ok"):
        subscribed_apps_test["error"] = subscriptions_raw.get("error")

    businesses_raw = await _meta_graph_get("me/businesses", fields="id,name")
    business_rows = (
        (businesses_raw.get("data") or {}).get("data") or []
        if businesses_raw.get("ok")
        else []
    )
    businesses = [
        {
            "id": str(item.get("id")),
            "name": item.get("name"),
            "source": "me.businesses",
        }
        for item in business_rows
        if isinstance(item, dict) and item.get("id")
    ]
    if META_BUSINESS_ID and all(
        item["id"] != META_BUSINESS_ID for item in businesses
    ):
        businesses.append({
            "id": META_BUSINESS_ID,
            "name": None,
            "source": "environment",
        })

    candidates = _granular_waba_candidates(token_check)
    token_data = token_check.get("data") if token_check.get("ok") else {}
    system_user_id = (
        token_data.get("system_user_id")
        or (
            token_data.get("user_id")
            if str(token_data.get("type") or "").upper() == "SYSTEM_USER"
            else None
        )
    ) if isinstance(token_data, dict) else None
    assigned_wabas: dict[str, Any] | None = None
    if system_user_id:
        assigned_raw = await _meta_graph_get(
            f"{system_user_id}/assigned_whatsapp_business_accounts",
            fields="id,name",
        )
        assigned_wabas = {
            "system_user_id": str(system_user_id),
            "http_status": assigned_raw.get("http_status"),
            "accessible": bool(assigned_raw.get("ok")),
        }
        if assigned_raw.get("ok"):
            assigned_rows = (assigned_raw.get("data") or {}).get("data") or []
            assigned_wabas["waba_count"] = len(assigned_rows)
            for item in assigned_rows:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                candidates.append({
                    "id": str(item["id"]),
                    "name": item.get("name"),
                    "source": "system_user.assigned_whatsapp_business_accounts",
                    "business_id": None,
                })
        else:
            assigned_wabas["error"] = assigned_raw.get("error")
    collection_attempts: list[dict[str, Any]] = []
    for business in businesses:
        for edge in (
            "owned_whatsapp_business_accounts",
            "client_whatsapp_business_accounts",
        ):
            collection_raw = await _meta_graph_get(
                f"{business['id']}/{edge}", fields="id,name"
            )
            attempt: dict[str, Any] = {
                "business_id": business["id"],
                "business_name": business.get("name"),
                "edge": edge,
                "http_status": collection_raw.get("http_status"),
                "accessible": bool(collection_raw.get("ok")),
            }
            if not collection_raw.get("ok"):
                attempt["error"] = collection_raw.get("error")
                collection_attempts.append(attempt)
                continue
            collection_data = collection_raw.get("data") or {}
            rows = collection_data.get("data") or []
            attempt["waba_count"] = len(rows)
            collection_attempts.append(attempt)
            for item in rows:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                candidates.append({
                    "id": str(item["id"]),
                    "name": item.get("name"),
                    "source": edge,
                    "business_id": business["id"],
                })
    candidates = _unique_waba_candidates(candidates)

    attempts: list[dict[str, Any]] = [{
        "candidate_id": KNOWN_DIAGNOSTIC_WABA_ID,
        "candidate_name": known_waba_data.get("name"),
        "source": "known_waba_test",
        "business_id": None,
        "http_status": known_numbers_raw.get("http_status"),
        "accessible": bool(known_numbers_raw.get("ok")),
        "contains_phone_number_id": known_contains_phone,
        **(
            {"error": known_numbers_raw.get("error")}
            if not known_numbers_raw.get("ok")
            else {}
        ),
    }]
    resolved_waba: dict[str, Any] | None = (
        {
            "ok": True,
            "resolved": True,
            "id": KNOWN_DIAGNOSTIC_WABA_ID,
            "name": known_waba_data.get("name"),
            "phone_number_id": PHONE_NUMBER_ID,
            "source": "known_waba_test",
            "business_id": None,
            "phone_numbers_http_status": known_numbers_raw["http_status"],
            "phone_numbers_accessible": True,
        }
        if known_contains_phone
        else None
    )
    for candidate in ([] if resolved_waba else candidates):
        candidate_id = str(candidate["id"])
        numbers_raw = await _meta_graph_get(
            f"{candidate_id}/phone_numbers",
            fields="id,display_phone_number,verified_name,quality_rating",
        )
        attempt: dict[str, Any] = {
            "candidate_id": candidate_id,
            "candidate_name": candidate.get("name"),
            "source": candidate.get("source"),
            "business_id": candidate.get("business_id"),
            "http_status": numbers_raw.get("http_status"),
            "accessible": bool(numbers_raw.get("ok")),
            "contains_phone_number_id": False,
        }
        if not numbers_raw.get("ok"):
            attempt["error"] = numbers_raw.get("error")
            attempts.append(attempt)
            continue
        numbers_data = numbers_raw.get("data") or {}
        phone_numbers = numbers_data.get("data") or []
        contains_phone = any(
            isinstance(item, dict)
            and str(item.get("id") or "") == PHONE_NUMBER_ID
            for item in phone_numbers
        )
        attempt["contains_phone_number_id"] = contains_phone
        attempts.append(attempt)
        if not contains_phone:
            continue
        resolved_waba = {
            "ok": True,
            "resolved": True,
            "id": candidate_id,
            "name": candidate.get("name"),
            "phone_number_id": PHONE_NUMBER_ID,
            "source": candidate.get("source"),
            "business_id": candidate.get("business_id"),
            "phone_numbers_http_status": numbers_raw["http_status"],
            "phone_numbers_accessible": True,
        }
        break

    waba_check = resolved_waba or {
        "ok": False,
        "resolved": False,
        "reason": (
            "Meta no devolvió WABA candidatas mediante los Business accesibles "
            "ni mediante granular_scopes."
            if not attempts
            else "Ninguna WABA candidata accesible contiene el Phone Number ID."
        ),
        "attempts": attempts,
    }
    business_discovery = {
        "ok": bool(businesses_raw.get("ok")),
        "http_status": businesses_raw.get("http_status"),
        "configured_business_id": META_BUSINESS_ID or None,
        "system_user_assigned_wabas": assigned_wabas,
        "businesses": businesses,
        "waba_collection_attempts": collection_attempts,
    }
    if not businesses_raw.get("ok"):
        business_discovery["error"] = businesses_raw.get("error")
    return {
        "graph_api_version": GRAPH_API_VERSION,
        "phone_number_id": PHONE_NUMBER_ID,
        "token": token_check,
        "permissions": permissions,
        "phone_number": phone_check,
        "known_waba_test": known_waba_test,
        "subscribed_apps_test": subscribed_apps_test,
        "business_discovery": business_discovery,
        "waba": waba_check,
    }


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
