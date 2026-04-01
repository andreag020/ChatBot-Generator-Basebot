import asyncio
import httpx
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

WHATSAPP_API_URL = "https://graph.facebook.com/v18.0"
MAX_MESSAGE_CHARS = 900
MAX_MESSAGE_PARTS = 2


class WhatsAppClient:
    def __init__(self):
        self.headers = {
            "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        }
        self.phone_id = settings.WHATSAPP_PHONE_ID

    async def send_text(self, to: str, text: str) -> list[dict]:
        chunks = self._split_text(text, MAX_MESSAGE_CHARS)
        chunks = chunks[:MAX_MESSAGE_PARTS]
        results: list[dict] = []

        for idx, chunk in enumerate(chunks):
            payload = {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": chunk, "preview_url": False},
            }
            results.append(await self._post(payload))
            if idx < len(chunks) - 1:
                await asyncio.sleep(0.35)

        return results

    async def send_buttons(self, to: str, body: str, buttons: list[dict]) -> dict:
        interactive_buttons = [
            {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
            for b in buttons[:3]
        ]
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body[:MAX_MESSAGE_CHARS]},
                "action": {"buttons": interactive_buttons},
            },
        }
        return await self._post(payload)

    async def _post(self, payload: dict) -> dict:
        url = f"{WHATSAPP_API_URL}/{self.phone_id}/messages"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, headers=self.headers, json=payload)
            if resp.status_code >= 400:
                logger.error("WhatsApp API error %s: %s", resp.status_code, resp.text)
            resp.raise_for_status()
            return resp.json()

    def _split_text(self, text: str, max_chars: int) -> list[str]:
        text = (text or "").strip()
        if not text:
            return ["Lo siento, no pude generar una respuesta."]
        if len(text) <= max_chars:
            return [text]

        parts: list[str] = []
        current = ""

        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
        if not paragraphs:
            paragraphs = [text]

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
                chunk = paragraph[:split_at].strip()
                parts.append(chunk)
                paragraph = paragraph[split_at:].strip()

            current = paragraph

        if current:
            parts.append(current)

        if len(parts) > MAX_MESSAGE_PARTS:
            kept = parts[:MAX_MESSAGE_PARTS]
            kept[-1] = kept[-1][: max_chars - 3].rstrip() + "..."
            return kept

        return parts


_whatsapp_client = WhatsAppClient()


async def send_whatsapp_message(to: str, text: str) -> list[dict]:
    return await _whatsapp_client.send_text(to, text)
