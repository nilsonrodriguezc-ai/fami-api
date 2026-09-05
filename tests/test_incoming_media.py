from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("VERIFY_TOKEN", "verify-test")
os.environ.setdefault("META_ACCESS_TOKEN", "meta-test")
os.environ.setdefault("PHONE_NUMBER_ID", "phone-test")
os.environ.setdefault("INTERNAL_API_TOKEN", "internal-test")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

import main
import media_service
from fastapi.testclient import TestClient


class IncomingMediaTests(unittest.TestCase):
    def _message(
        self,
        *,
        message_id: str = "wamid.media",
        message_type: str = "image",
        mime_type: str = "image/jpeg",
        filename: str | None = None,
    ) -> dict:
        media = {"id": "media-1", "mime_type": mime_type, "caption": "Prueba"}
        if filename:
            media["filename"] = filename
        return {
            "id": message_id,
            "from": "51999999999",
            "timestamp": "1700000000",
            "type": message_type,
            message_type: media,
        }

    def test_incoming_text_and_emoji_are_unchanged(self) -> None:
        message = {
            "id": "wamid.text", "from": "51999999999",
            "timestamp": "1700000000", "type": "text",
            "text": {"body": "Hola 🧸✨"},
        }
        with patch.object(
            main, "save_incoming_text", return_value=(3, True)
        ) as save:
            asyncio.run(main._process_incoming_message(message, {}))
        self.assertEqual(save.call_args.kwargs["text_body"], "Hola 🧸✨")
        self.assertEqual(save.call_args.kwargs["raw_payload"], message)

    def test_supported_media_is_stored_ready(self) -> None:
        cases = [
            ("image", "image/jpeg", b"\xff\xd8\xffjpeg", None),
            ("image", "image/png", b"\x89PNG\r\n\x1a\npng", None),
            ("document", "application/pdf", b"%PDF-1.7 pdf", "comprobante.pdf"),
        ]
        for message_type, mime_type, content, filename in cases:
            with self.subTest(mime_type=mime_type):
                message = self._message(
                    message_id=f"wamid.{mime_type}",
                    message_type=message_type,
                    mime_type=mime_type,
                    filename=filename,
                )
                stored = media_service.StoredMedia(
                    mime_type=mime_type,
                    storage_bucket="whatsapp-media",
                    storage_path="whatsapp/3/wamid/file",
                    size_bytes=len(content),
                    sha256="abc",
                )
                with (
                    patch.object(main, "save_incoming_media", return_value=(3, 9, True)) as save,
                    patch.object(main, "fetch_meta_media", new_callable=AsyncMock, return_value=(content, mime_type, len(content))),
                    patch.object(main, "store_private_media", new_callable=AsyncMock, return_value=stored),
                    patch.object(main, "update_incoming_media") as update,
                ):
                    asyncio.run(main._process_incoming_message(message, {}))
                self.assertEqual(save.call_args.kwargs["raw_payload"], message)
                self.assertEqual(update.call_args.kwargs["media_status"], "ready")

    def test_oversize_is_rejected_and_notified_once(self) -> None:
        message = self._message()
        too_large = media_service.MediaProcessingError(
            "file_too_large", "Supera 10 MB", size_bytes=10 * 1024 * 1024 + 1
        )
        with (
            patch.object(main, "save_incoming_media", return_value=(3, 9, True)),
            patch.object(main, "fetch_meta_media", new_callable=AsyncMock, side_effect=too_large),
            patch.object(main, "update_incoming_media") as update,
            patch.object(main, "enviar_mensaje", new_callable=AsyncMock, return_value={"messages": [{"id": "wamid.notice"}]}) as send,
            patch.object(main, "save_outgoing_text") as save_outgoing,
        ):
            asyncio.run(main._process_incoming_message(message, {}))
        self.assertEqual(update.call_args.kwargs["media_status"], "rejected")
        self.assertEqual(update.call_args.kwargs["error_code"], "file_too_large")
        send.assert_awaited_once()
        self.assertTrue(save_outgoing.call_args.kwargs["automated"])

        with (
            patch.object(main, "save_incoming_media", return_value=(3, None, False)),
            patch.object(main, "fetch_meta_media", new_callable=AsyncMock) as fetch,
            patch.object(main, "enviar_mensaje", new_callable=AsyncMock) as duplicate_send,
        ):
            asyncio.run(main._process_incoming_message(message, {}))
        fetch.assert_not_awaited()
        duplicate_send.assert_not_awaited()

    def test_unsupported_mime_is_rejected_without_notice(self) -> None:
        message = self._message(mime_type="image/webp")
        with (
            patch.object(main, "save_incoming_media", return_value=(3, 9, True)),
            patch.object(main, "fetch_meta_media", new_callable=AsyncMock) as fetch,
            patch.object(main, "update_incoming_media") as update,
            patch.object(main, "enviar_mensaje", new_callable=AsyncMock) as send,
        ):
            asyncio.run(main._process_incoming_message(message, {}))
        self.assertEqual(update.call_args.kwargs["media_status"], "rejected")
        fetch.assert_not_awaited()
        send.assert_not_awaited()

    def test_download_failure_is_failed(self) -> None:
        message = self._message()
        error = media_service.MediaProcessingError(
            "meta_download_failed", "Descarga fallida"
        )
        with (
            patch.object(main, "save_incoming_media", return_value=(3, 9, True)),
            patch.object(main, "fetch_meta_media", new_callable=AsyncMock, side_effect=error),
            patch.object(main, "update_incoming_media") as update,
        ):
            asyncio.run(main._process_incoming_message(message, {}))
        self.assertEqual(update.call_args.kwargs["media_status"], "failed")

    def test_storage_failure_is_failed(self) -> None:
        message = self._message()
        error = media_service.MediaProcessingError(
            "storage_upload_failed", "Storage falló"
        )
        with (
            patch.object(main, "save_incoming_media", return_value=(3, 9, True)),
            patch.object(main, "fetch_meta_media", new_callable=AsyncMock, return_value=(b"\xff\xd8\xffx", "image/jpeg", 4)),
            patch.object(main, "store_private_media", new_callable=AsyncMock, side_effect=error),
            patch.object(main, "update_incoming_media") as update,
        ):
            asyncio.run(main._process_incoming_message(message, {}))
        self.assertEqual(update.call_args.kwargs["media_status"], "failed")

    def test_duplicate_does_not_download_or_store(self) -> None:
        message = self._message()
        with (
            patch.object(main, "save_incoming_media", return_value=(3, None, False)),
            patch.object(main, "fetch_meta_media", new_callable=AsyncMock) as fetch,
            patch.object(main, "store_private_media", new_callable=AsyncMock) as store,
        ):
            asyncio.run(main._process_incoming_message(message, {}))
        fetch.assert_not_awaited()
        store.assert_not_awaited()


class MediaValidationTests(unittest.TestCase):
    def test_magic_bytes_for_allowed_types(self) -> None:
        media_service.validate_content(b"\xff\xd8\xffjpeg", "image/jpeg")
        media_service.validate_content(b"\x89PNG\r\n\x1a\npng", "image/png")
        media_service.validate_content(b"%PDF-1.7", "application/pdf")

    def test_mime_spoofing_is_rejected(self) -> None:
        with self.assertRaises(media_service.MediaProcessingError) as context:
            media_service.validate_content(b"not-a-pdf", "application/pdf")
        self.assertEqual(context.exception.code, "content_type_mismatch")

    def test_filename_is_sanitized(self) -> None:
        result = media_service.safe_filename("../../mi comprobante.PDF", "id", "application/pdf")
        self.assertEqual(result, "mi-comprobante.pdf")
        self.assertNotIn("..", result)


class _StorageClient:
    response: httpx.Response | None = None
    request_error: httpx.RequestError | None = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> "_StorageClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, *args: object, **kwargs: object) -> httpx.Response:
        if self.request_error is not None:
            raise self.request_error
        assert self.response is not None
        return self.response


class StorageObservabilityTests(unittest.TestCase):
    secret = "service-role-secret-must-not-leak"
    object_path = "whatsapp/3/wamid/documento-seguro.pdf"

    def _run_upload(self) -> media_service.MediaProcessingError:
        with (
            patch.object(media_service, "SUPABASE_URL", "https://project.supabase.co"),
            patch.object(media_service, "SUPABASE_SERVICE_ROLE_KEY", self.secret),
            patch.object(media_service, "WHATSAPP_MEDIA_BUCKET", "whatsapp-media"),
            patch.object(media_service.httpx, "AsyncClient", _StorageClient),
        ):
            with self.assertRaises(media_service.MediaProcessingError) as context:
                asyncio.run(media_service.store_private_media(
                    content=b"%PDF-1.7 test",
                    mime_type="application/pdf",
                    storage_path=self.object_path,
                ))
        return context.exception

    def test_http_errors_are_sanitized_and_keep_functional_error(self) -> None:
        for status in (401, 403, 404, 409):
            with self.subTest(status=status):
                request = httpx.Request(
                    "POST",
                    "https://project.supabase.co/storage/v1/object/whatsapp-media/path",
                )
                _StorageClient.request_error = None
                _StorageClient.response = httpx.Response(
                    status,
                    request=request,
                    json={
                        "statusCode": str(status),
                        "code": "StorageError",
                        "error": "Upload rejected",
                        "message": (
                            f"Authorization: Bearer {self.secret} "
                            f"apikey={self.secret} {self.object_path}"
                        ),
                    },
                )
                with self.assertLogs("fami-api.media", level="ERROR") as logs:
                    error = self._run_upload()
                output = "\n".join(logs.output)
                self.assertEqual(error.code, "storage_upload_failed")
                self.assertIn(f"status={status}", output)
                self.assertIn("StorageError", output)
                self.assertNotIn(self.secret, output)
                self.assertNotIn(self.object_path, output)
                self.assertNotIn("https://project.supabase.co", output)

    def test_connection_error_logs_only_safe_type_and_message(self) -> None:
        request = httpx.Request(
            "POST",
            "https://project.supabase.co/storage/v1/object/whatsapp-media/path",
        )
        _StorageClient.response = None
        _StorageClient.request_error = httpx.ConnectError(
            f"Connection failed token={self.secret} {self.object_path}",
            request=request,
        )
        with self.assertLogs("fami-api.media", level="ERROR") as logs:
            error = self._run_upload()
        output = "\n".join(logs.output)
        self.assertEqual(error.code, "storage_upload_failed")
        self.assertIn("type=ConnectError", output)
        self.assertNotIn(self.secret, output)
        self.assertNotIn(self.object_path, output)

    def tearDown(self) -> None:
        _StorageClient.response = None
        _StorageClient.request_error = None


class MediaEndpointTests(unittest.TestCase):
    def test_media_endpoint_requires_internal_token(self) -> None:
        response = TestClient(main.app).get("/internal/whatsapp/media/42")
        self.assertEqual(response.status_code, 401)

    def test_media_endpoint_returns_private_bytes(self) -> None:
        with (
            patch.object(main, "get_message_media", return_value={
                "message_id": 42,
                "mime_type": "application/pdf",
                "safe_filename": "comprobante.pdf",
                "storage_bucket": "whatsapp-media",
                "storage_path": "whatsapp/1/wamid/comprobante.pdf",
                "media_status": "ready",
            }),
            patch.object(
                main,
                "download_private_media",
                new_callable=AsyncMock,
                return_value=b"%PDF-1.7 test",
            ),
        ):
            response = TestClient(main.app).get(
                "/internal/whatsapp/media/42",
                headers={"X-Internal-Token": "internal-test"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"%PDF-1.7 test")
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertEqual(response.headers["cache-control"], "private, no-store")


if __name__ == "__main__":
    unittest.main()
