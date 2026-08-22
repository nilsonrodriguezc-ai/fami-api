from __future__ import annotations

import asyncio
import importlib
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


os.environ.setdefault("VERIFY_TOKEN", "verify-test")
os.environ.setdefault("META_ACCESS_TOKEN", "meta-test")
os.environ.setdefault("PHONE_NUMBER_ID", "phone-test")
os.environ.setdefault("INTERNAL_API_TOKEN", "internal-test")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

main = importlib.import_module("main")


class WebhookTests(unittest.TestCase):
    def test_catalog_still_works_before_database_is_configured(self) -> None:
        message = {
            "id": "wamid.catalog", "from": "51999999999",
            "timestamp": "1700000000", "type": "text",
            "text": {"body": "Quiero ver el catalogo"},
        }
        with (
            patch.object(main, "DATABASE_URL", ""),
            patch.object(
                main, "enviar_mensaje", new_callable=AsyncMock,
                return_value={"messages": [{"id": "wamid.auto"}]},
            ) as send,
        ):
            asyncio.run(main._process_incoming_message(message, {}))
        send.assert_awaited_once()

    def test_duplicate_message_does_not_trigger_catalog_response(self) -> None:
        message = {
            "id": "wamid.duplicate", "from": "51999999999",
            "timestamp": "1700000000", "type": "text",
            "text": {"body": "Quiero ver el catalogo"},
        }
        with (
            patch.object(main, "save_incoming_text", return_value=(7, False)),
            patch.object(main, "enviar_mensaje", new_callable=AsyncMock) as send,
        ):
            asyncio.run(main._process_incoming_message(message, {}))
        send.assert_not_awaited()

    def test_status_event_updates_existing_message(self) -> None:
        payload = {
            "entry": [{"changes": [{"value": {
                "statuses": [{"id": "wamid.1", "status": "read"}]
            }}]}]
        }
        with patch.object(main, "update_message_status", return_value=True) as update:
            asyncio.run(main.process_webhook_payload(payload))
        update.assert_called_once_with("wamid.1", "read")

    def test_webhook_acknowledges_valid_payload(self) -> None:
        payload = {"entry": [{"changes": [{"value": {"messages": []}}]}]}
        with patch.object(main, "process_webhook_payload", new_callable=AsyncMock):
            response = TestClient(main.app).post("/webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "recibido"})


class InternalEndpointTests(unittest.TestCase):
    def test_rejects_missing_internal_token(self) -> None:
        response = TestClient(main.app).post(
            "/internal/whatsapp/send-text",
            json={
                "phone_number": "+51999999999", "conversation_id": 4,
                "text": "Hola",
                "user_id": "00000000-0000-0000-0000-000000000001",
            },
        )
        self.assertEqual(response.status_code, 401)

    def test_valid_operator_is_recorded_from_database(self) -> None:
        request = {
            "phone_number": "+51999999999", "conversation_id": 4,
            "text": "Hola",
            "user_id": "00000000-0000-0000-0000-000000000001",
        }
        operator = {
            "id": request["user_id"],
            "nombre_completo": "Cynthia Operadora", "rol": "REGISTRO",
        }
        with (
            patch.object(main, "get_operator", return_value=operator),
            patch.object(main, "get_conversation", return_value={
                "id": 4, "phone_number": "+51999999999"
            }),
            patch.object(
                main, "enviar_mensaje", new_callable=AsyncMock,
                return_value={"messages": [{"id": "wamid.outgoing"}]},
            ),
            patch.object(main, "save_outgoing_text") as save,
            patch.object(main, "save_audit"),
        ):
            response = TestClient(main.app).post(
                "/internal/whatsapp/send-text",
                headers={"X-Internal-Token": "internal-test"}, json=request,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sent_by_user_name"], "Cynthia Operadora")
        self.assertEqual(save.call_args.kwargs["sent_by_user_id"], operator["id"])
        self.assertEqual(save.call_args.kwargs["sent_by_user_name"], "Cynthia Operadora")


if __name__ == "__main__":
    unittest.main()
