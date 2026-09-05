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
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

from database import (
    DATABASE_URL, get_conversation, get_operator, normalize_phone,
    get_message_media, save_audit, save_incoming_media, save_incoming_text,
    save_outgoing_text, update_incoming_media, update_message_status,
)
from media_service import (
    ALLOWED_MIME_TYPES, MediaProcessingError, download_private_media,
    fetch_meta_media, safe_filename, store_private_media,
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


def _build_cloud_api_registration_recovery(
    *,
    registration_status: Any,
    name_status: Any,
    platform_type: Any,
    account_mode: Any,
    certificate_present: bool,
    raw_checks: dict[str, Any],
) -> dict[str, Any]:
    registration = str(registration_status or "").upper() or "unknown"
    name_state = str(name_status or "").upper() or "unknown"
    platform = str(platform_type or "").upper() or "unknown"
    mode = str(account_mode or "").upper() or "unknown"
    is_on_premise = platform == "ON_PREMISE"
    is_cloud = platform == "CLOUD_API"
    migration_required: bool | str = (
        True if is_on_premise else False if is_cloud else "unknown"
    )
    register_supported: bool | str = (
        True if is_on_premise or is_cloud else "unknown"
    )
    if is_on_premise:
        recommended_action = (
            "Confirmar primero si existe acceso al backup de la antigua "
            "implementación On-Premise y al PIN de seis dígitos. Si existen, "
            "el mecanismo documentado es migrar mediante /register con PIN y "
            "backup. Si no existen, reanudar el alta del mismo número desde "
            "WhatsApp Manager/Embedded Signup seleccionando la WABA existente "
            "antes de intentar un registro Cloud."
        )
        blocking_reason = (
            "El activo figura como ON_PREMISE y las consultas de solo lectura "
            "no permiten confirmar la disponibilidad de backup.data, "
            "backup.password ni del PIN. No debe ejecutarse un registro simple "
            "hasta aclarar ese origen."
        )
    elif is_cloud and registration not in {"VERIFIED", "REGISTERED"}:
        recommended_action = (
            "Tras confirmar la propiedad del número y el PIN, el mecanismo "
            "documentado es registrar el Phone Number ID mediante /register."
        )
        blocking_reason = (
            "El número aún no figura verificado/registrado para Cloud API."
        )
    elif is_cloud:
        recommended_action = (
            "El número ya aparece en Cloud API; /register no debería ejecutarse "
            "sin revisar primero el error específico vigente."
        )
        blocking_reason = None
    else:
        recommended_action = (
            "Confirmar la modalidad del número en WhatsApp Manager o repetir "
            "el onboarding de solo configuración antes de cualquier POST."
        )
        blocking_reason = (
            "Meta no devolvió un platform_type que permita elegir entre "
            "registro Cloud y migración On-Premise."
        )
    return {
        "current_state": {
            "registration_status": registration,
            "name_status": name_state,
            "platform_type": platform,
            "hosting_type": platform,
            "account_mode": mode,
            "certificate_present": bool(certificate_present),
        },
        "expected_state": {
            "registration_status": "VERIFIED/REGISTERED",
            "platform_type": "CLOUD_API",
            "hosting_type": "CLOUD_API",
        },
        "register_endpoint_supported": register_supported,
        "pin_required": True,
        "migration_required": migration_required,
        "backup_required": True if is_on_premise else False if is_cloud else "unknown",
        "certificate_required": False,
        "smb_mode_detected": "unknown",
        "recommended_action": recommended_action,
        "exact_endpoint_if_applicable": (
            f"POST https://graph.facebook.com/{GRAPH_API_VERSION}/"
            f"{PHONE_NUMBER_ID}/register"
            if register_supported is True
            else None
        ),
        "required_payload_fields": (
            ["messaging_product", "pin", "backup.data", "backup.password"]
            if is_on_premise
            else ["messaging_product", "pin"] if is_cloud else []
        ),
        "blocking_reason": blocking_reason,
        "read_only_only": True,
        "raw_checks_sanitized": _sanitize_diagnostic_value(raw_checks),
    }


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
    if not message_id or not phone:
        logger.info("Mensaje entrante incompleto ignorado")
        return
    if message_type in {"image", "document"}:
        await _process_incoming_media(
            message=message,
            contacts=contacts,
            message_type=message_type,
            message_id=message_id,
            phone=phone,
        )
        return
    if message_type != "text":
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
    logger.info(
        "Mensaje entrante persistido conversation_id=%s inserted=%s",
        conversation_id,
        str(inserted).lower(),
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


async def _process_incoming_media(
    *,
    message: dict[str, Any],
    contacts: dict[str, str],
    message_type: str,
    message_id: str,
    phone: str,
) -> None:
    media = message.get(message_type) or {}
    if not isinstance(media, dict):
        media = {}
    meta_media_id = str(media.get("id") or "")
    declared_mime = str(media.get("mime_type") or "").lower() or None
    original_filename = (
        str(media.get("filename") or "") or None
        if message_type == "document"
        else None
    )
    caption = str(media.get("caption") or "") or None
    filename = safe_filename(original_filename, meta_media_id or message_id, declared_mime)
    if not DATABASE_URL:
        logger.warning("No se guardó multimedia: DATABASE_URL no configurada")
        return
    conversation_id, message_db_id, inserted = save_incoming_media(
        whatsapp_message_id=message_id,
        phone=phone,
        wa_id=phone,
        display_name=contacts.get(phone),
        message_type=message_type,
        meta_media_id=meta_media_id,
        mime_type=declared_mime,
        original_filename=original_filename,
        safe_filename=filename,
        caption=caption,
        whatsapp_timestamp=message.get("timestamp"),
        raw_payload=message,
    )
    logger.info(
        "Mensaje multimedia persistido conversation_id=%s inserted=%s",
        conversation_id,
        str(inserted).lower(),
    )
    if not inserted or message_db_id is None:
        logger.info("Mensaje multimedia duplicado ignorado id=%s", message_id)
        return
    if not meta_media_id:
        update_incoming_media(
            message_db_id,
            media_status="failed",
            error_code="missing_media_id",
            error_detail="Meta no incluyó un identificador multimedia.",
        )
        return
    if declared_mime and declared_mime not in ALLOWED_MIME_TYPES:
        update_incoming_media(
            message_db_id,
            media_status="rejected",
            mime_type=declared_mime,
            error_code="unsupported_mime_type",
            error_detail="El tipo de archivo no está permitido.",
        )
        logger.info(
            "Multimedia rechazada antes de descargar id=%s code=%s",
            message_id,
            "unsupported_mime_type",
        )
        return
    try:
        content, actual_mime, reported_size = await fetch_meta_media(
            media_id=meta_media_id,
            access_token=META_ACCESS_TOKEN,
            graph_api_version=GRAPH_API_VERSION,
        )
        if declared_mime and declared_mime != actual_mime:
            raise MediaProcessingError(
                "mime_type_mismatch",
                "El tipo informado por el webhook no coincide con Meta.",
                size_bytes=reported_size or len(content),
            )
        if actual_mime not in ALLOWED_MIME_TYPES:
            raise MediaProcessingError(
                "unsupported_mime_type", "El tipo de archivo no está permitido."
            )
        filename = safe_filename(
            original_filename, meta_media_id or message_id, actual_mime
        )
        message_path_id = re.sub(r"[^A-Za-z0-9._-]", "-", message_id)[:180]
        storage_path = (
            f"whatsapp/{conversation_id}/{message_path_id}/{filename}"
        )
        stored = await store_private_media(
            content=content,
            mime_type=actual_mime,
            storage_path=storage_path,
        )
        update_incoming_media(
            message_db_id,
            media_status="ready",
            mime_type=stored.mime_type,
            safe_filename=filename,
            storage_bucket=stored.storage_bucket,
            storage_path=stored.storage_path,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
        )
    except MediaProcessingError as error:
        rejected = error.code in {
            "file_too_large", "unsupported_mime_type",
            "mime_type_mismatch", "content_type_mismatch",
        }
        update_incoming_media(
            message_db_id,
            media_status="rejected" if rejected else "failed",
            size_bytes=error.size_bytes,
            error_code=error.code,
            error_detail=error.detail,
        )
        logger.warning(
            "Multimedia no procesada id=%s code=%s", message_id, error.code
        )
        if error.code == "file_too_large":
            automatic_text = (
                "El archivo que enviaste supera el límite permitido de 10 MB "
                "📎. Por favor, envíalo nuevamente en un tamaño menor."
            )
            try:
                result = await enviar_mensaje(phone, automatic_text)
                save_outgoing_text(
                    whatsapp_message_id=_message_id(result),
                    conversation_id=conversation_id,
                    phone=phone,
                    text_body=automatic_text,
                    sent_by_user_id=None,
                    sent_by_user_name="Automatización de archivos",
                    automated=True,
                    raw_payload=result,
                )
            except RuntimeError:
                logger.exception(
                    "No se pudo enviar el aviso de archivo demasiado grande"
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
    assigned_raw: dict[str, Any] | None = None
    assigned_rows: list[Any] = []
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
    client_waba_ids: set[str] = set()
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
                if edge == "client_whatsapp_business_accounts":
                    client_waba_ids.add(str(item["id"]))
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

    authority_owner_raw = await _meta_graph_get(
        KNOWN_DIAGNOSTIC_WABA_ID,
        fields="id,name,owner_business,owner_business_info",
    )
    authority_on_behalf_raw = await _meta_graph_get(
        KNOWN_DIAGNOSTIC_WABA_ID, fields="on_behalf_of_business_info"
    )
    authority_relationship_raw = await _meta_graph_get(
        KNOWN_DIAGNOSTIC_WABA_ID,
        fields=(
            "is_shared_with_partners,ownership_type,status,"
            "account_review_status,business_verification_status"
        ),
    )
    authority_data: dict[str, Any] = {}
    for check in (
        authority_owner_raw,
        authority_on_behalf_raw,
        authority_relationship_raw,
    ):
        if check.get("ok") and isinstance(check.get("data"), dict):
            authority_data.update(check["data"])
    authority_raw = {
        "data": _sanitize_diagnostic_value(authority_data),
        "checks": {
            "owner": authority_owner_raw,
            "on_behalf_of_business": authority_on_behalf_raw,
            "relationship_and_status": authority_relationship_raw,
        },
    }
    owner_info = authority_data.get("owner_business_info") or {}
    if not isinstance(owner_info, dict):
        owner_info = {}
    owner_business_id = str(owner_info.get("id") or "") or None
    checked_collection_business_ids = {
        str(item.get("business_id"))
        for item in collection_attempts
        if item.get("business_id")
    }
    if owner_business_id and owner_business_id not in checked_collection_business_ids:
        businesses.append({
            "id": owner_business_id,
            "name": owner_info.get("name"),
            "source": "waba.owner_business_info",
        })
        for edge in (
            "owned_whatsapp_business_accounts",
            "client_whatsapp_business_accounts",
        ):
            collection_raw = await _meta_graph_get(
                f"{owner_business_id}/{edge}", fields="id,name"
            )
            attempt = {
                "business_id": owner_business_id,
                "business_name": owner_info.get("name"),
                "edge": edge,
                "http_status": collection_raw.get("http_status"),
                "accessible": bool(collection_raw.get("ok")),
                "source": "waba.owner_business_info",
            }
            if collection_raw.get("ok"):
                rows = (collection_raw.get("data") or {}).get("data") or []
                attempt["waba_count"] = len(rows)
                if edge == "client_whatsapp_business_accounts":
                    client_waba_ids.update(
                        str(item.get("id"))
                        for item in rows
                        if isinstance(item, dict) and item.get("id")
                    )
            else:
                attempt["error"] = collection_raw.get("error")
            collection_attempts.append(attempt)
    business_ids_for_assignment = []
    for possible_id in (
        owner_business_id,
        META_BUSINESS_ID or None,
        *(business.get("id") for business in businesses),
    ):
        cleaned_id = str(possible_id or "")
        if cleaned_id and cleaned_id not in business_ids_for_assignment:
            business_ids_for_assignment.append(cleaned_id)

    assigned_user_checks: list[dict[str, Any]] = []
    matched_user: dict[str, Any] | None = None
    for business_id in business_ids_for_assignment:
        query = urllib.parse.urlencode({"business": business_id})
        users_raw = await _meta_graph_get(
            f"{KNOWN_DIAGNOSTIC_WABA_ID}/assigned_users?{query}",
            fields="id,name,tasks",
        )
        check: dict[str, Any] = {
            "business_id": business_id,
            "http_status": users_raw.get("http_status"),
            "accessible": bool(users_raw.get("ok")),
        }
        if users_raw.get("ok"):
            user_rows = (users_raw.get("data") or {}).get("data") or []
            check["assigned_user_count"] = len(user_rows)
            for item in user_rows:
                if not isinstance(item, dict):
                    continue
                if str(item.get("id") or "") == str(system_user_id or ""):
                    matched_user = {
                        "id": str(item.get("id")),
                        "name": item.get("name"),
                        "tasks": [str(task) for task in item.get("tasks") or []],
                        "business_id": business_id,
                    }
                    check["system_user_found"] = True
                    check["system_user_tasks"] = matched_user["tasks"]
                    break
            else:
                check["system_user_found"] = False
        else:
            check["error"] = users_raw.get("error")
        assigned_user_checks.append(check)

    direct_ids = {
        str(item.get("id"))
        for item in assigned_rows
        if isinstance(item, dict) and item.get("id")
    }
    if assigned_raw is None or not assigned_raw.get("ok"):
        directly_assigned: bool | str = "unknown"
    else:
        directly_assigned = KNOWN_DIAGNOSTIC_WABA_ID in direct_ids

    client_attempts = [
        item for item in collection_attempts
        if item.get("edge") == "client_whatsapp_business_accounts"
    ]
    client_candidate_found = KNOWN_DIAGNOSTIC_WABA_ID in client_waba_ids
    if client_candidate_found:
        client_assigned: bool | str = True
    elif any(item.get("accessible") for item in client_attempts):
        client_assigned = False
    else:
        client_assigned = "unknown"

    if matched_user is not None:
        messaging_task: bool | str = "MESSAGING" in {
            task.upper() for task in matched_user["tasks"]
        }
    elif any(item.get("accessible") for item in assigned_user_checks):
        messaging_task = False
    else:
        messaging_task = "unknown"

    partner_shared = authority_data.get("is_shared_with_partners")
    on_behalf_info = authority_data.get("on_behalf_of_business_info")
    partner_text = json.dumps(
        on_behalf_info or {}, ensure_ascii=False, default=str
    ).casefold()
    if "respond.io" in partner_text or RESPOND_IO_APP_ID in partner_text:
        respond_relationship: bool | str = True
    elif partner_shared is False and not subscribed_apps_test[
        "respond_io_subscribed"
    ]:
        respond_relationship = False
    else:
        respond_relationship = "unknown"

    if directly_assigned is False and messaging_task is False:
        conclusion = (
            "La WABA no aparece asignada directamente al System User y no "
            "se detectó la tarea MESSAGING. Este resultado es compatible con "
            "el rechazo 403 de envío."
        )
    elif directly_assigned is True and messaging_task is True:
        conclusion = (
            "La asignación directa y la tarea MESSAGING fueron detectadas. "
            "Estas comprobaciones no explican por sí solas el rechazo 403."
        )
    else:
        conclusion = (
            "Meta no permitió confirmar de forma completa la asignación y la "
            "tarea MESSAGING; revisa los checks marcados como unknown."
        )

    effective_messaging_access = {
        "system_user_id": str(system_user_id) if system_user_id else None,
        "waba_id": KNOWN_DIAGNOSTIC_WABA_ID,
        "phone_number_id": PHONE_NUMBER_ID,
        "app_id": fami_app_id,
        "owner_business_id": owner_business_id,
        "waba_directly_assigned": directly_assigned,
        "waba_client_assigned": client_assigned,
        "messaging_task_detected": messaging_task,
        "respond_io_relationship_detected": respond_relationship,
        "conclusion": conclusion,
        "raw_checks_sanitized": {
            "waba_authority": authority_raw,
            "system_user_assigned_wabas": assigned_raw,
            "waba_assigned_users": assigned_user_checks,
            "business_waba_collections": collection_attempts,
            "fami_app_subscribed": subscribed_apps_test["fami_subscribed"],
            "respond_io_app_subscribed": subscribed_apps_test[
                "respond_io_subscribed"
            ],
            "is_shared_with_partners": partner_shared,
            "on_behalf_of_business_info": _sanitize_diagnostic_value(
                on_behalf_info
            ),
        },
    }

    phone_registration_raw = await _meta_graph_get(
        PHONE_NUMBER_ID,
        fields=(
            "id,display_phone_number,verified_name,quality_rating,"
            "code_verification_status"
        ),
    )
    phone_name_status_raw = await _meta_graph_get(
        PHONE_NUMBER_ID, fields="name_status"
    )
    phone_platform_raw = await _meta_graph_get(
        PHONE_NUMBER_ID, fields="platform_type"
    )
    phone_certificate_raw = await _meta_graph_get(
        PHONE_NUMBER_ID, fields="certificate"
    )
    phone_registration_data: dict[str, Any] = {}
    for check in (
        phone_registration_raw,
        phone_name_status_raw,
        phone_platform_raw,
    ):
        if check.get("ok") and isinstance(check.get("data"), dict):
            phone_registration_data.update(check["data"])
    certificate_data = (
        phone_certificate_raw.get("data") or {}
        if phone_certificate_raw.get("ok")
        else {}
    )
    certificate_value = (
        certificate_data.get("certificate")
        if isinstance(certificate_data, dict)
        else None
    )
    certificate_summary = {
        "http_status": phone_certificate_raw.get("http_status"),
        "readable": bool(phone_certificate_raw.get("ok")),
        "certificate_present": bool(certificate_value),
    }
    if not phone_certificate_raw.get("ok"):
        certificate_summary["error"] = phone_certificate_raw.get("error")

    on_behalf_value = authority_data.get("on_behalf_of_business_info")
    partner_business_ids: list[str] = []

    def collect_business_ids(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = str(key).casefold()
                if normalized_key in {"id", "business_id"} and item:
                    candidate_id = str(item)
                    if candidate_id not in partner_business_ids:
                        partner_business_ids.append(candidate_id)
                elif isinstance(item, (dict, list)):
                    collect_business_ids(item)
        elif isinstance(value, list):
            for item in value:
                collect_business_ids(item)

    collect_business_ids(on_behalf_value)
    owner_business_value = authority_data.get("owner_business")
    owner_business_from_field = (
        str(owner_business_value.get("id") or "")
        if isinstance(owner_business_value, dict)
        else str(owner_business_value or "")
    ) or None
    effective_owner_business_id = owner_business_id or owner_business_from_field
    partner_business_ids = [
        item for item in partner_business_ids
        if item != effective_owner_business_id
    ]

    relationship_text = json.dumps(
        {
            "on_behalf": on_behalf_value,
            "ownership_type": authority_data.get("ownership_type"),
        },
        ensure_ascii=False,
        default=str,
    ).casefold()
    is_shared = authority_data.get("is_shared_with_partners")
    if (
        bool(on_behalf_value)
        or bool(partner_business_ids)
        or is_shared is True
        or "partner" in relationship_text
        or "bsp" in relationship_text
    ):
        provider_detected: bool | str = True
    elif (
        authority_on_behalf_raw.get("ok")
        and authority_relationship_raw.get("ok")
        and is_shared is False
    ):
        provider_detected = False
    else:
        provider_detected = "unknown"

    respond_evidence_text = json.dumps(
        {
            "on_behalf": on_behalf_value,
            "partner_business_ids": partner_business_ids,
        },
        ensure_ascii=False,
        default=str,
    ).casefold()
    if "respond.io" in respond_evidence_text or RESPOND_IO_APP_ID in (
        respond_evidence_text
    ):
        respond_detected: bool | str = True
    elif provider_detected is False:
        respond_detected = False
    else:
        respond_detected = "unknown"

    registration_status = phone_registration_data.get(
        "code_verification_status"
    )
    platform_type = phone_registration_data.get("platform_type")
    if respond_detected is True:
        provider_conclusion = (
            "Meta identifica explícitamente a respond.io en la relación del "
            "activo. Esta relación podría ser relevante para el 403, pero "
            "el diagnóstico no demuestra control exclusivo de envío."
        )
    elif provider_detected is True:
        provider_conclusion = (
            "Meta confirma una relación de partner/proveedor, pero los campos "
            "disponibles no identifican específicamente a respond.io. No es "
            "posible atribuir el 403 a respond.io solo con esta evidencia."
        )
    elif provider_detected is False:
        provider_conclusion = (
            "Los campos consultados no muestran una relación activa de "
            "partner/proveedor. Esta hipótesis no explica el 403."
        )
    else:
        provider_conclusion = (
            "Meta no expuso suficiente información para confirmar o descartar "
            "una relación operativa con un BSP. Revisa los errores sanitizados."
        )

    provider_relationship = {
        "owner_business_id": effective_owner_business_id,
        "owner_business_info": _sanitize_diagnostic_value(owner_info),
        "on_behalf_of_business_id": (
            partner_business_ids[0] if len(partner_business_ids) == 1 else None
        ),
        "on_behalf_of_business_info": _sanitize_diagnostic_value(
            on_behalf_value
        ),
        "partner_business_ids": partner_business_ids,
        "is_shared_with_partners": is_shared,
        "ownership_type": authority_data.get("ownership_type"),
        "bsp_or_solution_provider_detected": provider_detected,
        "respond_io_detected": respond_detected,
        "phone_registration_status": registration_status,
        "phone_name_status": phone_registration_data.get("name_status"),
        "platform_type": platform_type,
        "hosting_type": platform_type or "unknown",
        "certificate": certificate_summary,
        "conclusion": provider_conclusion,
        "waba_authority": authority_raw,
        "raw_checks_sanitized": {
            "phone_registration": phone_registration_raw,
            "phone_name_status": phone_name_status_raw,
            "phone_platform_type": phone_platform_raw,
            "phone_certificate": certificate_summary,
        },
    }
    phone_check_data = (
        phone_check.get("data") if phone_check.get("ok") else {}
    ) or {}
    cloud_api_registration_recovery = _build_cloud_api_registration_recovery(
        registration_status=registration_status,
        name_status=phone_registration_data.get("name_status"),
        platform_type=platform_type,
        account_mode=phone_check_data.get("account_mode"),
        certificate_present=bool(certificate_value),
        raw_checks={
            "phone_core": phone_raw,
            "account_mode": account_mode_check,
            "registration": phone_registration_raw,
            "name_status": phone_name_status_raw,
            "platform_type": phone_platform_raw,
            "certificate": certificate_summary,
        },
    )
    return {
        "graph_api_version": GRAPH_API_VERSION,
        "phone_number_id": PHONE_NUMBER_ID,
        "token": token_check,
        "permissions": permissions,
        "phone_number": phone_check,
        "known_waba_test": known_waba_test,
        "subscribed_apps_test": subscribed_apps_test,
        "business_discovery": business_discovery,
        "effective_messaging_access": effective_messaging_access,
        "provider_relationship": provider_relationship,
        "cloud_api_registration_recovery": cloud_api_registration_recovery,
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


@app.get("/internal/whatsapp/media/{message_id}")
async def get_internal_whatsapp_media(
    message_id: int,
    x_internal_token: str | None = Header(default=None),
) -> Response:
    """Return private media bytes only to an authenticated internal caller."""
    if not INTERNAL_API_TOKEN or not hmac.compare_digest(
        x_internal_token or "", INTERNAL_API_TOKEN
    ):
        raise HTTPException(status_code=401, detail="Acceso interno no autorizado")
    media = get_message_media(message_id)
    if not media:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    if media.get("media_status") != "ready":
        raise HTTPException(status_code=409, detail="El archivo aún no está disponible")
    bucket = str(media.get("storage_bucket") or "")
    storage_path = str(media.get("storage_path") or "")
    if not bucket or not storage_path:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    try:
        content = await download_private_media(bucket, storage_path)
    except MediaProcessingError as error:
        logger.warning(
            "No se recuperó multimedia message_id=%s code=%s",
            message_id,
            error.code,
        )
        raise HTTPException(
            status_code=502, detail="No se pudo recuperar el archivo."
        ) from error
    filename = str(media.get("safe_filename") or "archivo")
    return Response(
        content=content,
        media_type=str(media.get("mime_type") or "application/octet-stream"),
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
