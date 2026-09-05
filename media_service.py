from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass

import httpx


logger = logging.getLogger("fami-api.media")
MAX_MEDIA_BYTES = 10 * 1024 * 1024
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "application/pdf"}
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv(
    "SUPABASE_SERVICE_ROLE_KEY", ""
).strip()
WHATSAPP_MEDIA_BUCKET = os.getenv(
    "SUPABASE_WHATSAPP_MEDIA_BUCKET", "whatsapp-media"
).strip()
MAX_STORAGE_ERROR_TEXT = 300


class MediaProcessingError(RuntimeError):
    def __init__(self, code: str, detail: str, *, size_bytes: int | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.size_bytes = size_bytes


@dataclass(frozen=True)
class StoredMedia:
    mime_type: str
    storage_bucket: str
    storage_path: str
    size_bytes: int
    sha256: str


def safe_filename(filename: str | None, media_id: str, mime_type: str | None) -> str:
    extensions = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "application/pdf": ".pdf",
    }
    fallback = f"archivo-{re.sub(r'[^A-Za-z0-9_-]', '', media_id)[:32] or 'media'}"
    raw = os.path.basename(str(filename or fallback).replace("\\", "/"))
    stem = os.path.splitext(raw)[0]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("._-")[:80]
    return f"{stem or fallback}{extensions.get(str(mime_type or '').lower(), '')}"


def validate_content(content: bytes, mime_type: str) -> None:
    if mime_type == "image/jpeg" and content.startswith(b"\xff\xd8\xff"):
        return
    if mime_type == "image/png" and content.startswith(b"\x89PNG\r\n\x1a\n"):
        return
    if mime_type == "application/pdf" and content.lstrip().startswith(b"%PDF-"):
        return
    raise MediaProcessingError(
        "content_type_mismatch",
        "El contenido descargado no coincide con el tipo de archivo informado.",
        size_bytes=len(content),
    )


def _storage_headers() -> dict[str, str]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise MediaProcessingError(
            "storage_not_configured",
            "El almacenamiento privado de WhatsApp no está configurado.",
        )
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    }


def _sanitize_storage_log_text(
    value: object,
    *,
    storage_path: str = "",
    request_url: str = "",
) -> str:
    """Redact credentials and object locations before diagnostic logging."""
    text_value = str(value or "")
    for sensitive_value in (
        SUPABASE_SERVICE_ROLE_KEY,
        request_url,
        storage_path,
    ):
        if sensitive_value:
            text_value = text_value.replace(sensitive_value, "[REDACTED]")
    text_value = re.sub(
        r"(?i)((?:authorization|apikey|access[_-]?token|token)\s*[:=]\s*)"
        r"(?:bearer\s+)?[^\s,;}]+",
        r"\1[REDACTED]",
        text_value,
    )
    text_value = re.sub(r"https?://[^\s,;}]+", "[URL_REDACTED]", text_value)
    return text_value[:MAX_STORAGE_ERROR_TEXT]


async def fetch_meta_media(
    *, media_id: str, access_token: str, graph_api_version: str,
) -> tuple[bytes, str, int | None]:
    metadata_url = (
        f"https://graph.facebook.com/{graph_api_version}/{media_id}"
    )
    auth = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            metadata_response = await client.get(metadata_url, headers=auth)
            metadata_response.raise_for_status()
            metadata = metadata_response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise MediaProcessingError(
                "meta_metadata_failed",
                "Meta no permitió obtener los metadatos del archivo.",
            ) from error
        if not isinstance(metadata, dict):
            raise MediaProcessingError(
                "meta_metadata_failed",
                "Meta devolvió metadatos de archivo no válidos.",
            )
        download_url = str(metadata.get("url") or "")
        mime_type = str(metadata.get("mime_type") or "").lower()
        reported_size = metadata.get("file_size")
        try:
            reported_size = int(reported_size) if reported_size is not None else None
        except (TypeError, ValueError):
            reported_size = None
        if mime_type not in ALLOWED_MIME_TYPES:
            raise MediaProcessingError(
                "unsupported_mime_type",
                "El tipo de archivo no está permitido.",
                size_bytes=reported_size,
            )
        if reported_size is not None and reported_size > MAX_MEDIA_BYTES:
            raise MediaProcessingError(
                "file_too_large",
                "El archivo supera el límite permitido de 10 MB.",
                size_bytes=reported_size,
            )
        if not download_url.startswith("https://"):
            raise MediaProcessingError(
                "invalid_media_url", "Meta no devolvió una URL de descarga válida."
            )
        content = bytearray()
        try:
            async with client.stream("GET", download_url, headers=auth) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_MEDIA_BYTES:
                        raise MediaProcessingError(
                            "file_too_large",
                            "El archivo supera el límite permitido de 10 MB.",
                            size_bytes=len(content),
                        )
        except MediaProcessingError:
            raise
        except httpx.HTTPError as error:
            raise MediaProcessingError(
                "meta_download_failed", "No se pudo descargar el archivo desde Meta."
            ) from error
    payload = bytes(content)
    validate_content(payload, mime_type)
    return payload, mime_type, reported_size


async def store_private_media(
    *, content: bytes, mime_type: str, storage_path: str,
) -> StoredMedia:
    quoted_bucket = urllib.parse.quote(WHATSAPP_MEDIA_BUCKET, safe="")
    quoted_path = "/".join(
        urllib.parse.quote(part, safe="") for part in storage_path.split("/")
    )
    url = f"{SUPABASE_URL}/storage/v1/object/{quoted_bucket}/{quoted_path}"
    headers = _storage_headers()
    headers.update({"Content-Type": mime_type, "x-upsert": "false"})
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, headers=headers, content=content)
            response.raise_for_status()
    except httpx.HTTPStatusError as error:
        response = error.response
        try:
            response_data = response.json()
        except (ValueError, json.JSONDecodeError):
            response_data = None
        if isinstance(response_data, dict):
            safe_fields = {
                field: _sanitize_storage_log_text(
                    response_data.get(field),
                    storage_path=storage_path,
                    request_url=url,
                )
                for field in ("statusCode", "code", "error", "message")
                if field in response_data
            }
            logger.error(
                "Supabase Storage rechazó la subida status=%s details=%s",
                response.status_code,
                safe_fields,
            )
        else:
            safe_text = _sanitize_storage_log_text(
                response.text,
                storage_path=storage_path,
                request_url=url,
            )
            logger.error(
                "Supabase Storage rechazó la subida status=%s response=%s",
                response.status_code,
                safe_text,
            )
        raise MediaProcessingError(
            "storage_upload_failed", "No se pudo guardar el archivo privado."
        ) from error
    except httpx.RequestError as error:
        safe_message = _sanitize_storage_log_text(
            error,
            storage_path=storage_path,
            request_url=url,
        )
        logger.error(
            "Error de conexión con Supabase Storage type=%s message=%s",
            type(error).__name__,
            safe_message,
        )
        raise MediaProcessingError(
            "storage_upload_failed", "No se pudo guardar el archivo privado."
        ) from error
    return StoredMedia(
        mime_type=mime_type,
        storage_bucket=WHATSAPP_MEDIA_BUCKET,
        storage_path=storage_path,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


async def download_private_media(bucket: str, storage_path: str) -> bytes:
    if bucket != WHATSAPP_MEDIA_BUCKET:
        raise MediaProcessingError("invalid_bucket", "Bucket multimedia no válido.")
    quoted_bucket = urllib.parse.quote(bucket, safe="")
    quoted_path = "/".join(
        urllib.parse.quote(part, safe="") for part in storage_path.split("/")
    )
    url = f"{SUPABASE_URL}/storage/v1/object/{quoted_bucket}/{quoted_path}"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url, headers=_storage_headers())
            response.raise_for_status()
    except httpx.HTTPError as error:
        raise MediaProcessingError(
            "storage_download_failed", "No se pudo recuperar el archivo privado."
        ) from error
    if len(response.content) > MAX_MEDIA_BYTES:
        raise MediaProcessingError("file_too_large", "El archivo almacenado no es válido.")
    return response.content
