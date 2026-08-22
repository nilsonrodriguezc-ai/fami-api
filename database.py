from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if not DATABASE_URL:
        raise RuntimeError("Falta configurar DATABASE_URL en Render.")
    if _engine is None:
        url = DATABASE_URL
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        _engine = create_engine(
            url,
            pool_size=5,
            max_overflow=5,
            pool_pre_ping=True,
            pool_recycle=300,
            future=True,
        )
    return _engine


def _phone_digits(value: str) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())


def normalize_phone(value: str) -> str:
    digits = _phone_digits(value)
    if not 8 <= len(digits) <= 15:
        raise ValueError("El número de WhatsApp no es válido.")
    return f"+{digits}"


def _customer_id(connection: Any, phone: str) -> int | None:
    digits = _phone_digits(phone)
    local_digits = digits[2:] if digits.startswith("51") and len(digits) == 11 else digits
    value = connection.execute(
        text(
            """
            SELECT id
            FROM clientes
            WHERE regexp_replace(COALESCE(telefono, ''), '[^0-9]', '', 'g')
                  IN (:digits, :local_digits)
            ORDER BY activo DESC, id
            LIMIT 1
            """
        ),
        {"digits": digits, "local_digits": local_digits},
    ).scalar()
    return int(value) if value is not None else None


def save_incoming_text(
    *,
    whatsapp_message_id: str,
    phone: str,
    wa_id: str | None,
    display_name: str | None,
    text_body: str,
    whatsapp_timestamp: str | int | None,
    raw_payload: dict[str, Any],
) -> tuple[int, bool]:
    normalized_phone = normalize_phone(phone)
    timestamp = None
    if whatsapp_timestamp:
        timestamp = datetime.fromtimestamp(
            int(whatsapp_timestamp), tz=timezone.utc
        )
    with get_engine().begin() as connection:
        customer_id = _customer_id(connection, normalized_phone)
        conversation = connection.execute(
            text(
                """
                INSERT INTO whatsapp_conversations (
                    phone_number, wa_id, customer_id, display_name,
                    last_message_at, last_message_preview
                ) VALUES (
                    :phone, :wa_id, :customer_id, :display_name,
                    COALESCE(:message_at, NOW()), :preview
                )
                ON CONFLICT (phone_number) DO UPDATE SET
                    wa_id = COALESCE(EXCLUDED.wa_id, whatsapp_conversations.wa_id),
                    customer_id = COALESCE(
                        whatsapp_conversations.customer_id,
                        EXCLUDED.customer_id
                    ),
                    display_name = COALESCE(
                        EXCLUDED.display_name,
                        whatsapp_conversations.display_name
                    )
                RETURNING id
                """
            ),
            {
                "phone": normalized_phone,
                "wa_id": wa_id,
                "customer_id": customer_id,
                "display_name": display_name,
                "message_at": timestamp,
                "preview": text_body[:240],
            },
        ).scalar_one()
        inserted = connection.execute(
            text(
                """
                INSERT INTO whatsapp_messages (
                    whatsapp_message_id, conversation_id, phone_number,
                    direction, message_type, text_body, status,
                    whatsapp_timestamp, raw_payload
                ) VALUES (
                    :message_id, :conversation_id, :phone,
                    'incoming', 'text', :body, 'received',
                    :message_at, CAST(:raw_payload AS JSONB)
                )
                ON CONFLICT (whatsapp_message_id) DO NOTHING
                RETURNING id
                """
            ),
            {
                "message_id": whatsapp_message_id,
                "conversation_id": int(conversation),
                "phone": normalized_phone,
                "body": text_body,
                "message_at": timestamp,
                "raw_payload": __import__("json").dumps(raw_payload),
            },
        ).scalar()
        if inserted is not None:
            connection.execute(
                text(
                    """
                    UPDATE whatsapp_conversations
                    SET last_message_at = COALESCE(:message_at, NOW()),
                        last_message_preview = :preview,
                        unread_count = unread_count + 1,
                        status = CASE
                            WHEN status = 'resolved' THEN 'open'
                            ELSE status
                        END
                    WHERE id = :conversation_id
                    """
                ),
                {
                    "message_at": timestamp,
                    "preview": text_body[:240],
                    "conversation_id": int(conversation),
                },
            )
        return int(conversation), inserted is not None


def save_outgoing_text(
    *,
    whatsapp_message_id: str,
    conversation_id: int,
    phone: str,
    text_body: str,
    sent_by_user_id: str | None,
    sent_by_user_name: str | None,
    automated: bool,
    raw_payload: dict[str, Any] | None = None,
) -> None:
    normalized_phone = normalize_phone(phone)
    with get_engine().begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO whatsapp_messages (
                    whatsapp_message_id, conversation_id, phone_number,
                    direction, message_type, text_body, status,
                    sent_by_user_id, sent_by_user_name, is_automated,
                    whatsapp_timestamp, raw_payload
                ) VALUES (
                    :message_id, :conversation_id, :phone,
                    'outgoing', 'text', :body, 'sent',
                    :user_id, :user_name, :automated,
                    NOW(), CAST(:raw_payload AS JSONB)
                )
                ON CONFLICT (whatsapp_message_id) DO NOTHING
                """
            ),
            {
                "message_id": whatsapp_message_id,
                "conversation_id": conversation_id,
                "phone": normalized_phone,
                "body": text_body,
                "user_id": sent_by_user_id,
                "user_name": sent_by_user_name,
                "automated": automated,
                "raw_payload": __import__("json").dumps(raw_payload or {}),
            },
        )
        connection.execute(
            text(
                """
                UPDATE whatsapp_conversations
                SET last_message_at = NOW(),
                    last_message_preview = :preview
                WHERE id = :conversation_id
                """
            ),
            {"preview": text_body[:240], "conversation_id": conversation_id},
        )


def update_message_status(message_id: str, status: str) -> bool:
    allowed = {"sent", "delivered", "read", "failed"}
    normalized_status = str(status or "").lower()
    if normalized_status not in allowed:
        return False
    ranks = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}
    with get_engine().begin() as connection:
        current = connection.execute(
            text(
                """
                SELECT status FROM whatsapp_messages
                WHERE whatsapp_message_id = :message_id
                FOR UPDATE
                """
            ),
            {"message_id": message_id},
        ).scalar()
        if current is None:
            return False
        if ranks.get(str(current), 0) <= ranks[normalized_status]:
            connection.execute(
                text(
                    """
                    UPDATE whatsapp_messages SET status = :status
                    WHERE whatsapp_message_id = :message_id
                    """
                ),
                {"status": normalized_status, "message_id": message_id},
            )
        return True


def get_operator(user_id: str) -> dict[str, Any] | None:
    with get_engine().connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT id, nombre_completo, email, rol, activo
                FROM perfiles
                WHERE id = CAST(:user_id AS UUID) AND activo = TRUE
                LIMIT 1
                """
            ),
            {"user_id": user_id},
        ).mappings().first()
    return dict(row) if row else None


def get_conversation(conversation_id: int) -> dict[str, Any] | None:
    with get_engine().connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT id, phone_number, status, assigned_user_id
                FROM whatsapp_conversations
                WHERE id = :conversation_id
                """
            ),
            {"conversation_id": conversation_id},
        ).mappings().first()
    return dict(row) if row else None


def save_audit(
    *, user_id: str | None, conversation_id: int, action: str,
    new_data: dict[str, Any] | None = None,
) -> None:
    with get_engine().begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO auditoria (
                    usuario_id, entidad, entidad_id, accion, datos_nuevos
                ) VALUES (
                    CAST(:user_id AS UUID), 'whatsapp_conversation',
                    :entity_id, :action, CAST(:new_data AS JSONB)
                )
                """
            ),
            {
                "user_id": user_id,
                "entity_id": str(conversation_id),
                "action": action,
                "new_data": __import__("json").dumps(new_data or {}),
            },
        )
