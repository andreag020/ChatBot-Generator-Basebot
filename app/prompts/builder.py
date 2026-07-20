import hashlib
import logging
from pathlib import Path

import yaml

from app.core.config import settings

logger = logging.getLogger(__name__)

CORE_RULES = [
    "Never generate leads automatically or claim they have been registered.",
    "Never generate quotes, proposals, or specific prices.",
    "Never invent records, IDs, tickets, follow-ups, or non-existent confirmations.",
    "If the request requires commercial validation, follow-up, pricing, or human action, redirect to a human advisor.",
    "Respond only using the information available in this configuration and do not assume unconfirmed data."
]


def _normalize_channel_label(channel: str | None) -> str:
    normalized = str(channel or "").strip().lower()
    if normalized == "whatsapp":
        return "WhatsApp"
    if normalized == "instagram":
        return "Instagram"
    if normalized == "facebook":
        return "Facebook Messenger"
    if normalized == "telegram":
        return "Telegram"
    if normalized == "web":
        return "web chat"
    if normalized:
        return normalized.replace("_", " ")
    return "customer chat"


def assemble_system_prompt(
    config: dict | None,
    *,
    phone_number: str | None = None,
    default_bot_name: str | None = None,
    assistant_channel: str | None = None,
) -> str:
    c = dict(config or {})
    business = c.get("business", {})
    tone = c.get("tone", {})
    rules = c.get("rules", [])
    faqs = c.get("faqs", [])
    services = c.get("services", [])
    certifications = c.get("certifications", [])
    objectives = c.get("objectives", [])
    lead_fields = c.get("lead_fields", [])
    objection_guides = c.get("objection_guides", [])
    handoff = c.get("handoff", {})
    fallback = c.get("fallback", {})
    runtime = c.get("runtime", {})

    business_name = business.get("name", "Our Company")
    bot_name = business.get("bot_name", default_bot_name or settings.BOT_NAME)
    channel_label = _normalize_channel_label(
        assistant_channel or runtime.get("channel") or c.get("channel")
    )
    sections: list[str] = []

    sections.append(
        f"IDENTITY:\nYou are {bot_name}, the commercial assistant for {business_name}.\n"
        f"You are currently assisting through {channel_label}.\n"
        "Your function is to guide prospects and corporate clients, reply accurately using only "
        "the provided information, and escalate to a human advisor when the situation requires it."
    )
    sections.append(
        f"BUSINESS CONTEXT:\nName: {business_name}\n"
        f"Description: {business.get('description', '')}\n"
        f"Phone: {business.get('phone', '')}\n"
        f"WhatsApp: {business.get('whatsapp', '')}\n"
        f"Email: {business.get('email', '')}\n"
        f"Website: {business.get('website', '')}\n"
        f"Hours: {business.get('hours', '')}\n"
        f"Address: {business.get('address', '')}"
    )
    language = tone.get("language") or "formal english"
    language_mode = str(tone.get("language_mode") or "fixed").strip().lower()
    if language_mode not in {"fixed", "adaptive"}:
        language_mode = "fixed"

    if tone:
        sections.append(
            f"TONE AND STYLE:\n"
            f"- Personality: {tone.get('personality', 'professional')}\n"
            f"- Language: {language}"
            f"{' (default - see OUTPUT FORMAT for when to switch)' if language_mode == 'fixed' else ' (adaptive - always match the customer)'}\n"
            f"- Emojis: {'Do not use' if not tone.get('use_emojis') else 'Use with moderation'}\n"
            f"- Style: {tone.get('response_style', '')}"
        )
    if objectives:
        sections.append("MAIN OBJECTIVES:\n" + "\n".join(f"- {x}" for x in objectives))
    if services:
        lines = ["AVAILABLE SERVICES:"]
        for svc in services:
            line = f"- {svc['name']}: {svc.get('description', '').strip()}"
            if svc.get("sectors"):
                line += f" Sectors: {', '.join(svc['sectors'])}."
            if svc.get("modalities"):
                line += f" Modalities: {', '.join(svc['modalities'])}."
            lines.append(line)
        sections.append("\n".join(lines))
    if certifications:
        sections.append("CERTIFICATIONS:\n" + "\n".join(f"- {x}" for x in certifications))
    if faqs:
        lines = ["FREQUENTLY ASKED QUESTIONS (FAQS):"]
        for faq in faqs:
            lines.append(f"Q: {faq['question']}")
            lines.append(f"A: {faq['answer'].strip()}")
        sections.append("\n".join(lines))
    if objection_guides:
        lines = ["GUIDE FOR HANDLING OBJECTIONS:"]
        for item in objection_guides:
            lines.append(f"- If the client mentions '{item['trigger']}', reply: {item['response']}")
        sections.append("\n".join(lines))

    sections.append("CRITICAL IMMUTABLE RULES:\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(CORE_RULES, 1)))
    if rules:
        sections.append("ADDITIONAL CONFIGURABLE RULES:\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1)))

    lead_fields_text = ", ".join(lead_fields) if lead_fields else "name, company, service of interest, and scope"
    handoff_message = handoff.get(
        "message",
        f"Thank you for your message. An advisor from {business_name} will review your request and get back to you shortly.",
    )
    fallback_unknown = fallback.get("unknown_answer", handoff_message)
    fallback_out_of_scope = fallback.get(
        "out_of_scope",
        f"I can only help you with information strictly related to {business_name} and our services.",
    )

    sections.append(
        f"INTERNAL RESPONSE METHODOLOGY:\n"
        "1. Identify the intention: greeting, general information, commercial validation, objection, or out of scope.\n"
        "2. Use only the information contained in this prompt.\n"
        "3. If key info is missing, ask one brief question.\n"
        f"4. If the user wishes to be contacted by an advisor, only request these details if they help the context: {lead_fields_text}.\n"
        "5. Do not use tools or confirm automatic registrations.\n"
        "6. If the question requires pricing, a quote, a meeting, validation, or human management, escalate to the human advisor.\n"
        f"7. Suggested escalation message: {handoff_message}\n"
        f"8. If you do not know the exact answer, use this fallback: {fallback_unknown}\n"
        f"9. If the inquiry is outside the business scope, use this fallback: {fallback_out_of_scope}"
    )
    if language_mode == "adaptive":
        language_instruction = "- Always reply in the same language the customer is currently writing in."
    else:
        language_instruction = (
            f"- Default language: {language}. Reply in this language even if the customer writes in a different "
            "one. Only switch if the customer explicitly asks you to reply in another language "
            '(e.g. "can you answer in Spanish?"); once they ask, continue in that language for the rest of the '
            "conversation."
        )

    sections.append(
        "OUTPUT FORMAT:\n"
        f"{language_instruction}\n"
        "- Use clear, formal, and natural corporate tone. Maximum 4 lines.\n"
        "- No markdown. Avoid repeating information already given.\n"
        + ("- OBLIGATORY: Use attractive emojis strategically in your text to make it friendly.\n" if tone.get("use_emojis") else "- OBLIGATORY: NEVER use any emojis in your responses.\n")
        + "- Always end with a next step when it adds value.\n"
        f"- If the user just says hello, introduce {business_name} briefly and ask about their interest."
    )
    if phone_number:
        sections.append(f"CONTEXT: User's phone number: {phone_number}")
    sections.append(
        "SAFETY PHRASE: If you do not know the answer, offer to escalate to the commercial team using the official channel."
    )
    return "\n\n".join(s for s in sections if s.strip())


class PromptBuilder:
    """
    Builds the system prompt from bot_config.yaml.
    Auto-reloads on SHA-256 hash change (more reliable than mtime).
    """

    def __init__(self):
        self._config: dict = {}
        self._last_hash: str = ""
        self._config_path = Path(settings.CONFIG_PATH)
        self._load()

    def build(self, phone_number: str | None = None) -> str:
        self._reload_if_changed()
        return self._assemble(phone_number)

    def force_reload(self) -> bool:
        old_hash = self._last_hash
        self._load()
        return self._last_hash != old_hash

    @property
    def current_config(self) -> dict:
        self._reload_if_changed()
        return self._config

    def _file_hash(self) -> str:
        if not self._config_path.exists():
            return ""
        return hashlib.sha256(self._config_path.read_bytes()).hexdigest()

    def _reload_if_changed(self):
        current = self._file_hash()
        if current != self._last_hash:
            logger.info("bot_config.yaml changed — reloading")
            self._load()

    def _load(self):
        if not self._config_path.exists():
            logger.warning("Config not found at %s", self._config_path)
            self._config = {}
            self._last_hash = ""
            return
        try:
            raw = self._config_path.read_bytes()
            self._config = yaml.safe_load(raw.decode("utf-8")) or {}
            self._last_hash = hashlib.sha256(raw).hexdigest()
            logger.info("bot_config.yaml loaded (hash=%s)", self._last_hash[:8])
        except Exception as exc:
            logger.error("Failed to load bot_config.yaml: %s", exc)

    def _assemble(self, phone_number: str | None) -> str:
        return assemble_system_prompt(
            self._config,
            phone_number=phone_number,
            default_bot_name=settings.BOT_NAME,
        )
