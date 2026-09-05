from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import database


class _Result:
    def __init__(self, value=None):
        self.value = value

    def scalar(self):
        return self.value

    def scalar_one(self):
        return self.value


class _Connection:
    def __init__(self, *, inserted_id=99):
        self.inserted_id = inserted_id
        self.calls = []

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.calls.append((sql, parameters or {}))
        if "SELECT id" in sql and "FROM clientes" in sql:
            return _Result(None)
        if "INSERT INTO whatsapp_conversations" in sql:
            return _Result(3)
        if "INSERT INTO whatsapp_messages" in sql:
            return _Result(self.inserted_id)
        return _Result(None)


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback):
        return False


class _Engine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return _Transaction(self.connection)


class IncomingTimestampTests(unittest.TestCase):
    received_at = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)

    def _save(self, timestamp, *, inserted_id=99):
        connection = _Connection(inserted_id=inserted_id)
        raw_payload = {"timestamp": timestamp, "type": "text"}
        with (
            patch.object(database, "get_engine", return_value=_Engine(connection)),
            patch.object(database, "_utc_now", return_value=self.received_at),
        ):
            result = database.save_incoming_text(
                whatsapp_message_id="wamid.test",
                phone="51999999999",
                wa_id="51999999999",
                display_name="Cliente",
                text_body="Mensaje de prueba",
                whatsapp_timestamp=timestamp,
                raw_payload=raw_payload,
            )
        conversation_params = next(
            params for sql, params in connection.calls
            if "INSERT INTO whatsapp_conversations" in sql
        )
        message_sql, message_params = next(
            (sql, params) for sql, params in connection.calls
            if "INSERT INTO whatsapp_messages" in sql
        )
        updates = [
            params for sql, params in connection.calls
            if "UPDATE whatsapp_conversations" in sql
        ]
        return (
            result,
            connection,
            conversation_params,
            message_sql,
            message_params,
            updates,
            raw_payload,
        )

    def test_current_meta_timestamp_is_used_for_conversation(self):
        meta_time = self.received_at - timedelta(minutes=5)
        result = self._save(str(int(meta_time.timestamp())))
        _, _, conversation, _, message, updates, _ = result

        self.assertEqual(message["message_at"], meta_time)
        self.assertEqual(conversation["activity_at"], meta_time)
        self.assertEqual(updates[0]["activity_at"], meta_time)

    def test_historical_timestamp_is_preserved_but_not_used_for_ordering(self):
        historical = "1504902988"
        result = self._save(historical)
        _, _, conversation, message_sql, message, updates, raw = result

        self.assertEqual(message["message_at"].year, 2017)
        self.assertEqual(conversation["activity_at"], self.received_at)
        self.assertEqual(updates[0]["activity_at"], self.received_at)
        self.assertEqual(json.loads(message["raw_payload"]), raw)
        self.assertNotIn("created_at", message_sql.casefold())

    def test_missing_or_invalid_timestamp_uses_receipt_time(self):
        for value in (None, "not-a-timestamp"):
            with self.subTest(value=value):
                result = self._save(value)
                _, _, conversation, _, message, updates, _ = result
                self.assertIsNone(message["message_at"])
                self.assertEqual(conversation["activity_at"], self.received_at)
                self.assertEqual(updates[0]["activity_at"], self.received_at)

    def test_absurdly_future_timestamp_uses_receipt_time(self):
        future = self.received_at + timedelta(days=3650)
        result = self._save(str(int(future.timestamp())))
        _, _, conversation, _, message, updates, _ = result

        self.assertEqual(message["message_at"], future)
        self.assertEqual(conversation["activity_at"], self.received_at)
        self.assertEqual(updates[0]["activity_at"], self.received_at)

    def test_duplicate_does_not_increment_unread_count(self):
        result = self._save(
            str(int(self.received_at.timestamp())),
            inserted_id=None,
        )
        save_result, connection, _, _, _, updates, _ = result

        self.assertEqual(save_result, (3, False))
        self.assertEqual(updates, [])
        message_sql = next(
            sql for sql, _ in connection.calls
            if "INSERT INTO whatsapp_messages" in sql
        )
        self.assertIn(
            "ON CONFLICT (whatsapp_message_id) DO NOTHING",
            message_sql,
        )

    def test_duplicate_media_does_not_increment_unread_count(self):
        connection = _Connection(inserted_id=None)
        raw_payload = {
            "id": "wamid.media", "type": "image",
            "timestamp": str(int(self.received_at.timestamp())),
            "image": {"id": "media-1", "mime_type": "image/jpeg"},
        }
        with (
            patch.object(database, "get_engine", return_value=_Engine(connection)),
            patch.object(database, "_utc_now", return_value=self.received_at),
        ):
            result = database.save_incoming_media(
                whatsapp_message_id="wamid.media",
                phone="51999999999",
                wa_id="51999999999",
                display_name="Cliente",
                message_type="image",
                meta_media_id="media-1",
                mime_type="image/jpeg",
                original_filename=None,
                safe_filename="archivo.jpg",
                caption=None,
                whatsapp_timestamp=raw_payload["timestamp"],
                raw_payload=raw_payload,
            )
        self.assertEqual(result, (3, None, False))
        self.assertFalse(any(
            "UPDATE whatsapp_conversations" in sql
            for sql, _ in connection.calls
        ))
        message_sql, message_params = next(
            (sql, params) for sql, params in connection.calls
            if "INSERT INTO whatsapp_messages" in sql
        )
        self.assertIn("ON CONFLICT (whatsapp_message_id) DO NOTHING", message_sql)
        self.assertEqual(json.loads(message_params["raw_payload"]), raw_payload)


if __name__ == "__main__":
    unittest.main()
