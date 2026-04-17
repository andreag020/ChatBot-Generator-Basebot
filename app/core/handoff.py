from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

WHITESPACE_PATTERN = re.compile(r"\s+")
TRANSCRIPT_LIMIT = 8


@dataclass
class HandoffDecision:
    reason: str
    matched_value: str


def _normalize_text(value: str) -> str:
    return WHITESPACE_PATTERN.sub(" ", str(value or "").strip()).lower()


def _get_handoff_config(config: dict[str, Any] | None) -> dict[str, Any]:
    handoff = dict((config or {}).get("handoff") or {})
    handoff["enabled"] = bool(handoff.get("enabled", False))
    handoff["notify_team"] = bool(handoff.get("notify_team", False))
    handoff["message"] = str(handoff.get("message") or "").strip()
    handoff["triggers"] = [str(item or "").strip() for item in (handoff.get("triggers") or []) if str(item or "").strip()]
    handoff["notification_emails"] = [
        str(item or "").strip().lower()
        for item in (handoff.get("notification_emails") or [])
        if str(item or "").strip()
    ]
    return handoff


def handoff_notifications_enabled(config: dict[str, Any] | None) -> bool:
    handoff = _get_handoff_config(config)
    return bool(handoff.get("enabled") and handoff.get("notify_team") and handoff.get("notification_emails"))


def evaluate_handoff(
    config: dict[str, Any] | None,
    *,
    user_message: str,
    response_text: str,
) -> HandoffDecision | None:
    handoff = _get_handoff_config(config)
    if not handoff.get("enabled"):
        logger.info("Handoff evaluation skipped: handoff.enabled=false")
        return None

    normalized_user_message = _normalize_text(user_message)
    normalized_response_text = _normalize_text(response_text)

    for trigger in handoff.get("triggers", []):
        normalized_trigger = _normalize_text(trigger)
        if normalized_trigger and normalized_trigger in normalized_user_message:
            logger.info(
                "Handoff matched by trigger phrase: matched_value=%s",
                trigger,
            )
            return HandoffDecision(reason="trigger_phrase", matched_value=trigger)

    fallback = dict((config or {}).get("fallback") or {})
    for reason, candidate in (
        ("handoff_message", handoff.get("message")),
        ("unknown_answer", str(fallback.get("unknown_answer") or "").strip()),
        ("out_of_scope", str(fallback.get("out_of_scope") or "").strip()),
    ):
        normalized_candidate = _normalize_text(candidate)
        if normalized_candidate and normalized_candidate == normalized_response_text:
            logger.info(
                "Handoff matched by response equality: reason=%s matched_value=%s",
                reason,
                candidate,
            )
            return HandoffDecision(reason=reason, matched_value=candidate)

    logger.info(
        "Handoff not matched: triggers=%s normalized_response=%s",
        len(handoff.get("triggers", [])),
        normalized_response_text[:180],
    )
    return None


def build_transcript_excerpt(history: list[dict] | None, *, limit: int = TRANSCRIPT_LIMIT) -> str:
    transcript_lines: list[str] = []
    for item in history or []:
        role = item.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        speaker = "Customer" if role == "user" else "Bot"
        transcript_lines.append(f"{speaker}: {content}")

    if not transcript_lines:
        return "No recent transcript available."

    return "\n".join(transcript_lines[-limit:])


async def send_handoff_notification(
    config: dict[str, Any] | None,
    *,
    phone_number: str,
    user_message: str,
    response_text: str,
    history: list[dict] | None,
    decision: HandoffDecision,
) -> bool:
    handoff = _get_handoff_config(config)
    recipients = handoff.get("notification_emails") or []
    provider = settings.HANDOFF_EMAIL_PROVIDER.strip().lower()
    logger.info(
        "Handoff notification requested: provider=%s recipients=%s api_key_present=%s from_present=%s decision_reason=%s",
        provider or "(empty)",
        len(recipients),
        bool(settings.HANDOFF_EMAIL_API_KEY),
        bool(settings.HANDOFF_EMAIL_FROM),
        decision.reason,
    )
    if not recipients:
        logger.info("Handoff notification skipped: no recipients configured")
        return False

    if provider != "resend":
        logger.warning("Handoff notifications disabled: unsupported email provider '%s'", settings.HANDOFF_EMAIL_PROVIDER)
        return False

    if not settings.HANDOFF_EMAIL_API_KEY or not settings.HANDOFF_EMAIL_FROM:
        logger.warning("Handoff notifications disabled: HANDOFF_EMAIL_API_KEY or HANDOFF_EMAIL_FROM missing")
        return False

    business = dict((config or {}).get("business") or {})
    bot_name = str(business.get("bot_name") or business.get("name") or settings.BOT_NAME).strip()
    business_name = str(business.get("name") or bot_name or "Your business").strip()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    transcript_excerpt = build_transcript_excerpt(history)
    subject = f"[{business_name}] Human follow-up requested for {phone_number}"
    text = (
        f"Bot: {bot_name}\n"
        f"Business: {business_name}\n"
        f"Customer WhatsApp: {phone_number}\n"
        f"Timestamp: {timestamp}\n"
        f"Trigger reason: {decision.reason}\n"
        f"Matched value: {decision.matched_value}\n\n"
        f"Latest customer message:\n{user_message.strip() or '(empty)'}\n\n"
        f"Bot reply shown to customer:\n{response_text.strip() or '(empty)'}\n\n"
        f"Recent transcript:\n{transcript_excerpt}\n"
    )

    payload = {
        "from": settings.HANDOFF_EMAIL_FROM,
        "to": recipients,
        "subject": subject,
        "text": text,
    }

    try:
        logger.info("Calling Resend email API for handoff notification")
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings.HANDOFF_EMAIL_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if response.status_code >= 400:
                logger.error("Resend API error %s: %s", response.status_code, response.text)
            response.raise_for_status()
    except Exception:
        logger.exception("Failed to send handoff notification email for phone=%s", phone_number)
        return False

    logger.info("Handoff notification sent for phone=%s recipients=%s", phone_number, len(recipients))
    return True
