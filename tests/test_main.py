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


class MetaDiagnosticsTests(unittest.TestCase):
    def test_diagnostics_rejects_missing_internal_token(self) -> None:
        with patch.object(main, "_meta_graph_get", new_callable=AsyncMock) as get:
            response = TestClient(main.app).get("/internal/meta/diagnostics")
        self.assertEqual(response.status_code, 401)
        get.assert_not_awaited()

    def test_diagnostics_resolves_waba_without_exposing_secrets(self) -> None:
        token_result = {
            "ok": True,
            "http_status": 200,
            "data": {
                "is_valid": True,
                "app_id": "app-safe",
                "system_user_id": "system-user-safe",
                "scopes": ["whatsapp_business_messaging"],
                "granular_scopes": [],
                "type": "SYSTEM_USER",
            },
        }

        async def graph_result(
            object_path: str, *, fields: str | None = None,
        ) -> dict:
            if object_path == "me/permissions":
                return {
                    "ok": True, "http_status": 200,
                    "data": {"data": [{
                        "permission": "whatsapp_business_messaging",
                        "status": "granted",
                    }]},
                }
            if (
                object_path == main.KNOWN_DIAGNOSTIC_WABA_ID
                and fields
                and "owner_business_info" in fields
            ):
                return {
                    "ok": True, "http_status": 200,
                    "data": {
                        "id": main.KNOWN_DIAGNOSTIC_WABA_ID,
                        "name": "Aplicación de WhatsApp Business",
                        "owner_business_info": {
                            "id": "business-safe", "name": "Fami Business",
                        },
                        "is_shared_with_partners": True,
                    },
                }
            if (
                object_path == main.KNOWN_DIAGNOSTIC_WABA_ID
                and fields == "on_behalf_of_business_info"
            ):
                return {
                    "ok": True, "http_status": 200,
                    "data": {"on_behalf_of_business_info": {
                        "id": "partner-safe", "name": "respond.io",
                    }},
                }
            if (
                object_path == main.KNOWN_DIAGNOSTIC_WABA_ID
                and fields
                and "ownership_type" in fields
            ):
                return {
                    "ok": True, "http_status": 200,
                    "data": {
                        "is_shared_with_partners": True,
                        "ownership_type": "PARTNER_OWNED",
                        "status": "ACTIVE",
                    },
                }
            if object_path == main.KNOWN_DIAGNOSTIC_WABA_ID:
                return {
                    "ok": True, "http_status": 200,
                    "data": {
                        "id": main.KNOWN_DIAGNOSTIC_WABA_ID,
                        "name": "Aplicación de WhatsApp Business",
                    },
                }
            if object_path == f"{main.KNOWN_DIAGNOSTIC_WABA_ID}/phone_numbers":
                return {
                    "ok": True, "http_status": 200,
                    "data": {"data": [{
                        "id": "phone-test",
                        "display_phone_number": "+51 938 259 714",
                    }]},
                }
            if object_path == f"{main.KNOWN_DIAGNOSTIC_WABA_ID}/subscribed_apps":
                return {
                    "ok": True, "http_status": 200,
                    "data": {"data": [
                        {"whatsapp_business_api_data": {
                            "id": "app-safe", "name": "Fami",
                            "link": "https://example.test/fami",
                        }},
                        {
                            "override_callback_uri": (
                                "https://respond.example/webhook"
                            ),
                            "whatsapp_business_api_data": {
                                "id": main.RESPOND_IO_APP_ID,
                                "name": "respond.io WA BSP",
                            },
                        },
                    ]},
                }
            if object_path == "me/businesses":
                return {
                    "ok": True, "http_status": 200,
                    "data": {"data": [{
                        "id": "business-safe", "name": "Fami Business",
                    }]},
                }
            if object_path == "system-user-safe/assigned_whatsapp_business_accounts":
                return {
                    "ok": True, "http_status": 200,
                    "data": {"data": [{
                        "id": main.KNOWN_DIAGNOSTIC_WABA_ID,
                        "name": "Aplicación de WhatsApp Business",
                    }]},
                }
            if object_path == "business-safe/owned_whatsapp_business_accounts":
                return {
                    "ok": True, "http_status": 200,
                    "data": {"data": [{
                        "id": "waba-safe",
                        "name": "Aplicación de WhatsApp Business",
                    }]},
                }
            if object_path == "business-safe/client_whatsapp_business_accounts":
                return {
                    "ok": True, "http_status": 200,
                    "data": {"data": [{
                        "id": main.KNOWN_DIAGNOSTIC_WABA_ID,
                        "name": "Aplicación de WhatsApp Business",
                    }]},
                }
            if object_path == (
                f"{main.KNOWN_DIAGNOSTIC_WABA_ID}/assigned_users?"
                "business=business-safe"
            ):
                return {
                    "ok": True, "http_status": 200,
                    "data": {"data": [{
                        "id": "system-user-safe",
                        "name": "Employee",
                        "tasks": ["MANAGE", "MESSAGING"],
                    }]},
                }
            if object_path == "phone-test" and fields == "account_mode":
                return {
                    "ok": True, "http_status": 200,
                    "data": {"account_mode": "LIVE"},
                }
            if (
                object_path == "phone-test"
                and fields
                and "code_verification_status" in fields
            ):
                return {
                    "ok": True, "http_status": 200,
                    "data": {
                        "id": "phone-test",
                        "code_verification_status": "VERIFIED",
                    },
                }
            if object_path == "phone-test" and fields == "name_status":
                return {
                    "ok": True, "http_status": 200,
                    "data": {"name_status": "APPROVED"},
                }
            if object_path == "phone-test" and fields == "platform_type":
                return {
                    "ok": True, "http_status": 200,
                    "data": {"platform_type": "CLOUD_API"},
                }
            if object_path == "phone-test" and fields == "certificate":
                return {
                    "ok": True, "http_status": 200,
                    "data": {"certificate": "certificate-must-not-leak"},
                }
            if object_path == "phone-test":
                return {
                    "ok": True, "http_status": 200,
                    "data": {
                        "id": "phone-test",
                        "display_phone_number": "+51 938 259 714",
                        "verified_name": "Famalandia",
                        "quality_rating": "GREEN",
                    },
                }
            if object_path == "waba-safe/phone_numbers":
                return {
                    "ok": True, "http_status": 200,
                    "data": {"data": [{"id": "phone-test"}]},
                }
            raise AssertionError(f"Consulta no esperada: {object_path} {fields}")

        with (
            patch.object(
                main, "_debug_current_meta_token_sync",
                return_value=token_result,
            ),
            patch.object(
                main, "_meta_graph_get", side_effect=graph_result,
            ) as graph_get,
        ):
            response = TestClient(main.app).get(
                "/internal/meta/diagnostics",
                headers={"X-Internal-Token": "internal-test"},
            )

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result["token"]["data"]["is_valid"])
        self.assertEqual(
            result["waba"]["id"], main.KNOWN_DIAGNOSTIC_WABA_ID
        )
        self.assertEqual(
            result["waba"]["name"], "Aplicación de WhatsApp Business"
        )
        self.assertEqual(result["waba"]["phone_number_id"], "phone-test")
        self.assertTrue(result["waba"]["phone_numbers_accessible"])
        self.assertTrue(result["known_waba_test"]["waba_readable"])
        self.assertTrue(
            result["known_waba_test"]["phone_numbers_readable"]
        )
        self.assertTrue(
            result["known_waba_test"]["contains_expected_phone_number"]
        )
        self.assertEqual(
            result["known_waba_test"]["http_status"],
            {"waba": 200, "phone_numbers": 200},
        )
        subscriptions = result["subscribed_apps_test"]
        self.assertEqual(subscriptions["http_status"], 200)
        self.assertEqual(subscriptions["subscribed_app_count"], 2)
        self.assertTrue(subscriptions["more_than_one_app"])
        self.assertTrue(subscriptions["fami_subscribed"])
        self.assertTrue(subscriptions["respond_io_subscribed"])
        self.assertEqual(
            subscriptions["productive_webhook_app"]["app"]["id"],
            main.RESPOND_IO_APP_ID,
        )
        effective = result["effective_messaging_access"]
        self.assertEqual(effective["system_user_id"], "system-user-safe")
        self.assertEqual(effective["owner_business_id"], "business-safe")
        self.assertTrue(effective["waba_directly_assigned"])
        self.assertTrue(effective["waba_client_assigned"])
        self.assertTrue(effective["messaging_task_detected"])
        self.assertTrue(effective["respond_io_relationship_detected"])
        provider = result["provider_relationship"]
        self.assertEqual(provider["owner_business_id"], "business-safe")
        self.assertEqual(
            provider["on_behalf_of_business_id"], "partner-safe"
        )
        self.assertTrue(provider["bsp_or_solution_provider_detected"])
        self.assertTrue(provider["respond_io_detected"])
        self.assertEqual(provider["phone_registration_status"], "VERIFIED")
        self.assertEqual(provider["platform_type"], "CLOUD_API")
        self.assertTrue(provider["certificate"]["certificate_present"])
        self.assertFalse(any(
            call.kwargs.get("fields") == "whatsapp_business_account"
            for call in graph_get.await_args_list
        ))
        serialized = response.text.casefold()
        self.assertNotIn("meta-test", serialized)
        self.assertNotIn("internal-test", serialized)
        self.assertNotIn("postgresql://", serialized)
        self.assertNotIn("certificate-must-not-leak", serialized)

    def test_meta_error_redacts_sensitive_error_data(self) -> None:
        result = main._safe_meta_error(403, {
            "error": {
                "message": "Token meta-test was rejected",
                "type": "OAuthException",
                "code": 200,
                "error_subcode": 123,
                "error_data": {
                    "access_token": "meta-test",
                    "password": "secret-password",
                },
                "fbtrace_id": "trace-safe",
            }
        })
        serialized = str(result).casefold()
        self.assertNotIn("meta-test", serialized)
        self.assertNotIn("secret-password", serialized)
        self.assertIn("[redacted]", serialized)


if __name__ == "__main__":
    unittest.main()
