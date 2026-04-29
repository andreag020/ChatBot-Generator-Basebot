from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.whatsapp import send_whatsapp_message

META_CHANNELS = {"whatsapp", "instagram", "facebook"}
SUPPORTED_CHANNELS = META_CHANNELS | {"web"}
META_PAGE_CHANNELS = {"instagram", "facebook"}
GRAPH_API_URL = "https://graph.facebook.com/v18.0"


def normalize_channel(value: str | None, default: str = "whatsapp") -> str:
    channel = str(value or default).strip().lower() or default
    return channel if channel in SUPPORTED_CHANNELS else default


def is_meta_channel(channel: str | None) -> bool:
    return normalize_channel(channel) in META_CHANNELS


def channel_label(channel: str | None) -> str:
    normalized = normalize_channel(channel)
    if normalized == "whatsapp":
        return "WhatsApp"
    if normalized == "instagram":
        return "Instagram"
    if normalized == "facebook":
        return "Facebook Messenger"
    return "Web chat"


def customer_contact_label(channel: str | None) -> str:
    normalized = normalize_channel(channel)
    if normalized == "web":
        return "Web session"
    return f"Customer {channel_label(normalized)}"


def meta_verify_token() -> str:
    return str(settings.META_VERIFY_TOKEN or settings.WHATSAPP_VERIFY_TOKEN or "").strip()


def channel_requires_meta_page_credentials(channel: str | None) -> bool:
    return normalize_channel(channel) in META_PAGE_CHANNELS


@dataclass
class IncomingMessage:
    message_id: str
    sender_id: str
    text: str


def parse_incoming_message(channel: str | None, body: dict) -> tuple[str, IncomingMessage | None]:
    normalized = normalize_channel(channel)
    if normalized == "whatsapp":
        return _parse_whatsapp_message(body)
    if normalized in META_PAGE_CHANNELS:
        return _parse_page_messaging_message(body)
    return "unsupported_channel", None


def _parse_whatsapp_message(body: dict) -> tuple[str, IncomingMessage | None]:
    entries = body.get("entry") or []
    if not entries:
        return "no_entry", None

    entry = entries[0] if isinstance(entries[0], dict) else {}
    changes = entry.get("changes") or []
    if not changes:
        return "no_change", None

    change = changes[0] if isinstance(changes[0], dict) else {}
    value = change.get("value") or {}

    if "statuses" in value:
        return "ignored_status", None

    messages = value.get("messages") or []
    if not messages:
        return "no_message", None

    message = messages[0]
    message_type = message.get("type")
    if message_type != "text":
        return f"ignored_{message_type or 'unknown'}", None

    incoming = IncomingMessage(
        message_id=str(message.get("id") or "").strip(),
        sender_id=str(message.get("from") or "").strip(),
        text=str((message.get("text") or {}).get("body") or "").strip(),
    )
    if not incoming.message_id or not incoming.sender_id or not incoming.text:
        return "invalid_message", None
    return "ok", incoming


def _parse_page_messaging_message(body: dict) -> tuple[str, IncomingMessage | None]:
    entries = body.get("entry") or []
    if not entries:
        return "no_entry", None

    entry = entries[0] if isinstance(entries[0], dict) else {}
    messaging_items = entry.get("messaging") or []
    if not messaging_items:
        return "no_message", None

    event = messaging_items[0] if isinstance(messaging_items[0], dict) else {}
    if event.get("delivery") or event.get("read") or event.get("optin"):
        return "ignored_event", None

    message = event.get("message") or {}
    if message.get("is_echo"):
        return "ignored_echo", None

    text = str(message.get("text") or "").strip()
    message_id = str(message.get("mid") or message.get("id") or "").strip()
    sender_id = str((event.get("sender") or {}).get("id") or "").strip()

    if not text:
        return "ignored_non_text", None
    if not message_id or not sender_id:
        return "invalid_message", None

    return "ok", IncomingMessage(message_id=message_id, sender_id=sender_id, text=text)


async def send_channel_message(channel: str | None, recipient_id: str, text: str) -> list[dict]:
    normalized = normalize_channel(channel)
    if normalized == "whatsapp":
        return await send_whatsapp_message(recipient_id, text)
    if normalized in META_PAGE_CHANNELS:
        return await _send_page_messaging_text(recipient_id, text)
    raise ValueError(f"Unsupported outbound channel: {normalized}")


async def _send_page_messaging_text(recipient_id: str, text: str) -> list[dict]:
    token = str(settings.META_ACCESS_TOKEN or "").strip()
    page_id = str(settings.META_PAGE_ID or "").strip()
    if not token or not page_id:
        raise RuntimeError("META_ACCESS_TOKEN and META_PAGE_ID are required for Instagram and Facebook messaging.")

    chunks = _split_text(text, 900)
    results: list[dict] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for chunk in chunks[:2]:
            response = await client.post(
                f"{GRAPH_API_URL}/{page_id}/messages",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "recipient": {"id": recipient_id},
                    "messaging_type": "RESPONSE",
                    "message": {"text": chunk},
                },
            )
            response.raise_for_status()
            results.append(response.json())
    return results


def _split_text(text: str, max_chars: int) -> list[str]:
    message = (text or "").strip()
    if not message:
        return ["Sorry, I could not generate a response."]
    if len(message) <= max_chars:
        return [message]

    parts: list[str] = []
    current = ""
    paragraphs = [item.strip() for item in message.split("\n") if item.strip()] or [message]
    for paragraph in paragraphs:
        candidate = f"{current}\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        while len(paragraph) > max_chars:
            split_at = paragraph.rfind(" ", 0, max_chars)
            if split_at <= 0:
                split_at = max_chars
            parts.append(paragraph[:split_at].strip())
            paragraph = paragraph[split_at:].strip()
        current = paragraph
    if current:
        parts.append(current)
    return parts
