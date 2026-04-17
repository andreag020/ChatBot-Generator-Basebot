from collections import defaultdict
from datetime import datetime, timedelta
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

SESSION_TTL_MINUTES = 30
MAX_HISTORY_MESSAGES = settings.HISTORY_MAX_MESSAGES
PROCESSED_MESSAGE_TTL_MINUTES = 15
HANDOFF_NOTIFICATION_TTL_MINUTES = 30


class SessionStore:
    """
    In-memory session store with TTL.
    For production, replace with Redis:
      pip install redis
      self.redis = redis.Redis(...)
    """

    def __init__(self):
        self._sessions: dict[str, dict] = defaultdict(lambda: {"history": [], "last_seen": datetime.now()})

    def get(self, phone: str) -> list[dict]:
        """Return conversation history for a phone number."""
        self._evict_expired()
        session = self._sessions.get(phone)
        if session:
            session["last_seen"] = datetime.now()
            return session["history"]
        return []

    def set(self, phone: str, history: list[dict]):
        """Save conversation history, trimming if too long."""
        trimmed = history[-MAX_HISTORY_MESSAGES:] if len(history) > MAX_HISTORY_MESSAGES else history
        self._sessions[phone] = {
            "history": trimmed,
            "last_seen": datetime.now()
        }

    def clear(self, phone: str):
        """Clear session for a phone number."""
        self._sessions.pop(phone, None)

    def _evict_expired(self):
        """Remove sessions older than TTL."""
        cutoff = datetime.now() - timedelta(minutes=SESSION_TTL_MINUTES)
        expired = [k for k, v in self._sessions.items() if v["last_seen"] < cutoff]
        for k in expired:
            del self._sessions[k]
        if expired:
            logger.info("Evicted %s expired sessions", len(expired))


class ProcessedMessageStore:
    """Simple in-memory deduplication store for WhatsApp message IDs."""

    def __init__(self):
        self._processed: dict[str, datetime] = {}

    def is_processed(self, message_id: str) -> bool:
        self._evict_expired()
        return message_id in self._processed

    def mark_processed(self, message_id: str):
        self._evict_expired()
        self._processed[message_id] = datetime.now()

    def _evict_expired(self):
        cutoff = datetime.now() - timedelta(minutes=PROCESSED_MESSAGE_TTL_MINUTES)
        expired = [k for k, v in self._processed.items() if v < cutoff]
        for k in expired:
            del self._processed[k]
        if expired:
            logger.info("Evicted %s processed message ids", len(expired))


class HandoffNotificationStore:
    """Cooldown store to avoid duplicate human follow-up alerts."""

    def __init__(self):
        self._sent: dict[str, datetime] = {}

    def can_send(self, phone: str) -> bool:
        self._evict_expired()
        return phone not in self._sent

    def mark_sent(self, phone: str):
        self._evict_expired()
        self._sent[phone] = datetime.now()

    def _evict_expired(self):
        cutoff = datetime.now() - timedelta(minutes=HANDOFF_NOTIFICATION_TTL_MINUTES)
        expired = [k for k, v in self._sent.items() if v < cutoff]
        for k in expired:
            del self._sent[k]
        if expired:
            logger.info("Evicted %s handoff notification cooldowns", len(expired))
