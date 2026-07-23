import logging
import secrets
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from pydantic import BaseModel, Field

from app.core.ai_engine import AIEngine
from app.core.channels import (
    channel_label,
    customer_contact_label,
    is_meta_channel,
    meta_verify_token,
    normalize_channel,
    parse_incoming_message,
    send_channel_message,
    verify_meta_signature,
)
from app.core.config import settings
from app.core.handoff import evaluate_handoff, handoff_notifications_enabled, send_handoff_notification
from app.core.limits import DailyBudget, RateLimiter
from app.core.session import HandoffNotificationStore, SessionStore, ProcessedMessageStore
from app.routers import admin as admin_router
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="ChatbotAPI", version="1.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------
# Cloud Deploy Bootstrap via BOT_PAYLOAD_B64
# ---------------------------------------------------------
import os
import json
import base64
import yaml
from urllib.parse import unquote

payload_b64 = os.getenv("BOT_PAYLOAD_B64")
if payload_b64:
    try:
        logger.info("BOT_PAYLOAD_B64 detected. Bootstrapping configuration...")

        padding = len(payload_b64) % 4
        if padding > 0:
            payload_b64 += "=" * (4 - padding)

        decoded_json_str = base64.b64decode(payload_b64).decode("utf-8")
        payload = json.loads(unquote(decoded_json_str)) if "%" in decoded_json_str else json.loads(decoded_json_str)

        deploy_config = payload.pop("deploy", {})
        payload.pop("locale", None)

        if "provider" in deploy_config:
            settings.AI_PROVIDER = deploy_config["provider"]
            logger.info("[BOOTSTRAP] AI_PROVIDER set to: %s", settings.AI_PROVIDER)
        if "model" in deploy_config:
            DEPRECATED_MODELS = {
                "claude-3-haiku-20240307": "claude-haiku-4-5-20251001",
                "claude-3-5-haiku-20241022": "claude-haiku-4-5-20251001",
            }
            model_name = deploy_config["model"]
            if model_name in DEPRECATED_MODELS:
                new_model = DEPRECATED_MODELS[model_name]
                logger.warning(
                    "[BOOTSTRAP] Deprecated model '%s' auto-upgraded to '%s'",
                    model_name,
                    new_model,
                )
                deploy_config["model"] = new_model
            settings.AI_MODEL = deploy_config["model"]
            settings.OPENROUTER_MODEL = deploy_config["model"]
            settings.GEMINI_MODEL = deploy_config["model"]
            logger.info("[BOOTSTRAP] AI_MODEL set to: %s", settings.AI_MODEL)

        if "byoe_url" in deploy_config and deploy_config["byoe_url"]:
            settings.OPENROUTER_BASE_URL = deploy_config["byoe_url"]

        if "verify_token" in deploy_config:
            settings.META_VERIFY_TOKEN = deploy_config["verify_token"]
            settings.WHATSAPP_VERIFY_TOKEN = deploy_config["verify_token"]

        if "telegram_token" in deploy_config and deploy_config["telegram_token"]:
            settings.TELEGRAM_BOT_TOKEN = deploy_config["telegram_token"]
            logger.info("[BOOTSTRAP] TELEGRAM_BOT_TOKEN set from payload")

        if "webhook_base_url" in deploy_config and deploy_config["webhook_base_url"]:
            settings.TELEGRAM_WEBHOOK_BASE_URL = deploy_config["webhook_base_url"].rstrip("/")
            logger.info("[BOOTSTRAP] TELEGRAM_WEBHOOK_BASE_URL set to: %s", settings.TELEGRAM_WEBHOOK_BASE_URL)

        runtime = dict(payload.get("runtime") or {})
        if deploy_config.get("channel"):
            runtime["channel"] = deploy_config["channel"]
        if runtime:
            payload["runtime"] = runtime

        os.makedirs("config", exist_ok=True)
        with open("config/bot_config.yaml", "w", encoding="utf-8") as f:
            yaml.dump(payload, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        logger.info("Successfully applied BOT_PAYLOAD_B64 to memory and bot_config.yaml")
        logger.info(
            "[BOOTSTRAP] Final effective settings -> AI_PROVIDER=%s | AI_MODEL=%s | ANTHROPIC_KEY_SET=%s",
            settings.AI_PROVIDER,
            settings.AI_MODEL,
            bool(settings.ANTHROPIC_API_KEY),
        )
    except Exception as e:
        logger.error("Failed to bootstrap BOT_PAYLOAD_B64: %s", e)
# ---------------------------------------------------------

logger.info("[STARTUP] Initializing AIEngine with provider=%s model=%s", settings.AI_PROVIDER, settings.AI_MODEL)
ai_engine_instance = AIEngine()
logger.info(
    "[STARTUP] AIEngine ready. engine.provider=%s | client_set=%s",
    ai_engine_instance.provider,
    ai_engine_instance.anthropic_client is not None,
)
session_store = SessionStore()
processed_store = ProcessedMessageStore()
handoff_notification_store = HandoffNotificationStore()

# Abuse / cost controls — protect the managed AI key from unbounded spend.
inbound_rate_limiter = RateLimiter(settings.RATE_LIMIT_PER_MINUTE)
daily_budget = DailyBudget(settings.DAILY_MESSAGE_BUDGET)


# A shared secret Telegram echoes back in the X-Telegram-Bot-Api-Secret-Token
# header on every webhook call, so we can reject forged updates. If the operator
# did not supply one we generate a stable per-process value and register it with
# setWebhook during startup.
TELEGRAM_WEBHOOK_SECRET = settings.TELEGRAM_WEBHOOK_SECRET.strip() or secrets.token_hex(24)


def _within_limits(sender_key: str, user_text: str) -> bool:
    """Return True if this inbound message may be answered by the AI model."""
    if len(user_text or "") > settings.MAX_INBOUND_MESSAGE_CHARS > 0:
        logger.warning("Message rejected: exceeds MAX_INBOUND_MESSAGE_CHARS from %s", sender_key)
        return False
    if not inbound_rate_limiter.allow(sender_key):
        logger.warning("Message rejected: per-sender rate limit hit for %s", sender_key)
        return False
    if not daily_budget.allow():
        logger.warning("Message rejected: daily message budget (%s) exhausted", settings.DAILY_MESSAGE_BUDGET)
        return False
    daily_budget.record()
    return True

import app.core.ai_engine as _ae_module

_ae_module.ai_engine_instance = ai_engine_instance

app.include_router(admin_router.router)


async def _auto_setup_telegram():
    token = settings.TELEGRAM_BOT_TOKEN
    base_url = settings.TELEGRAM_WEBHOOK_BASE_URL
    runtime_channel = _runtime_channel()

    if not token or normalize_channel(runtime_channel) != "telegram":
        return

    import httpx as _httpx

    telegram_api = f"https://api.telegram.org/bot{token}"

    async with _httpx.AsyncClient(timeout=15.0) as client:
        # Validate token and retrieve bot identity
        me_resp = await client.get(f"{telegram_api}/getMe")
        me = me_resp.json()
        if not me.get("ok"):
            logger.error("[TELEGRAM SETUP] Invalid token — getMe failed: %s", me.get("description"))
            return
        bot_username = me["result"].get("username", "")
        logger.info("[TELEGRAM SETUP] Token valid. Bot username: @%s", bot_username)

        # Register webhook
        if base_url:
            webhook_url = f"{base_url}/telegram-webhook"
            wh_resp = await client.post(
                f"{telegram_api}/setWebhook",
                json={
                    "url": webhook_url,
                    "drop_pending_updates": True,
                    "secret_token": TELEGRAM_WEBHOOK_SECRET,
                },
            )
            wh = wh_resp.json()
            if wh.get("ok"):
                logger.info("[TELEGRAM SETUP] Webhook registered: %s", webhook_url)
            else:
                logger.error("[TELEGRAM SETUP] Webhook registration failed: %s", wh.get("description"))
        else:
            logger.warning("[TELEGRAM SETUP] TELEGRAM_WEBHOOK_BASE_URL not set — skipping webhook registration")

        # Sync bot profile from business config
        config = ai_engine_instance.prompt_builder.current_config
        business = dict((config or {}).get("business") or {})
        bot_name = str(business.get("bot_name") or business.get("name") or "").strip()
        description = str(business.get("description") or "").strip()[:512]

        if bot_name:
            await client.post(f"{telegram_api}/setMyName", json={"name": bot_name[:64]})
            logger.info("[TELEGRAM SETUP] Bot name set to: %s", bot_name)

        if description:
            await client.post(f"{telegram_api}/setMyDescription", json={"description": description})
            logger.info("[TELEGRAM SETUP] Bot description synced")


@app.on_event("startup")
async def on_startup():
    try:
        await _auto_setup_telegram()
    except Exception:
        logger.exception("[TELEGRAM SETUP] Auto-setup failed — bot will still work if token/webhook were previously configured")


class WebChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: str = Field(default="")


def _session_key(channel: str, identifier: str) -> str:
    return f"{channel}:{identifier}"


def _normalize_web_session_id(raw: str) -> str:
    cleaned = "".join(ch for ch in str(raw or "").strip() if ch.isalnum() or ch in {"-", "_"})
    cleaned = cleaned.strip("-_")
    return cleaned[:80] if cleaned else f"web-{secrets.token_hex(8)}"


def _runtime_channel() -> str:
    runtime = dict((ai_engine_instance.prompt_builder.current_config or {}).get("runtime") or {})
    return normalize_channel(runtime.get("channel") or "whatsapp")


async def _handle_conversation_turn(
    session_key: str,
    user_text: str,
    *,
    prompt_context: str,
) -> tuple[str, list[dict], dict, object | None]:
    history = session_store.get(session_key)
    response_text, updated_history = await ai_engine_instance.process(
        user_message=user_text,
        history=history,
        phone_number=prompt_context,
    )
    current_config = ai_engine_instance.prompt_builder.current_config
    if updated_history is not None:
        session_store.set(session_key, updated_history)
    decision = evaluate_handoff(
        current_config,
        user_message=user_text,
        response_text=response_text,
    )
    if decision:
        logger.info(
            "Handoff decision created: session=%s reason=%s matched_value=%s notify_enabled=%s",
            session_key,
            decision.reason,
            decision.matched_value,
            handoff_notifications_enabled(current_config),
        )
        response_text = _ensure_handoff_message(response_text, current_config)
    else:
        logger.info("No handoff decision created for session=%s", session_key)
    return response_text, updated_history, current_config, decision


def _ensure_handoff_message(response_text: str, config: dict) -> str:
    """Append the configured handoff message so escalation is always explicit to the customer,
    instead of relying on the model to mention it consistently on its own."""
    business_name = str((config.get("business") or {}).get("name") or "").strip() or "our team"
    handoff_message = str((config.get("handoff") or {}).get("message") or "").strip() or (
        f"Thank you for your message. An advisor from {business_name} will review your request and get back to you shortly."
    )
    if handoff_message.lower() in response_text.lower():
        return response_text
    return f"{response_text.rstrip()}\n\n{handoff_message}"


async def _maybe_send_handoff_email(
    *,
    notification_key: str,
    contact_value: str,
    contact_label: str,
    current_config: dict,
    user_text: str,
    response_text: str,
    updated_history: list[dict] | None,
    decision,
):
    if (
        decision
        and handoff_notifications_enabled(current_config)
        and handoff_notification_store.can_send(notification_key)
    ):
        sent = await send_handoff_notification(
            current_config,
            phone_number=contact_value,
            contact_label=contact_label,
            user_message=user_text,
            response_text=response_text,
            history=updated_history,
            decision=decision,
        )
        if sent:
            handoff_notification_store.mark_sent(notification_key)
    elif decision and not handoff_notifications_enabled(current_config):
        logger.info("Handoff decision not notified: notifications disabled or recipients missing for session=%s", notification_key)
    elif decision and not handoff_notification_store.can_send(notification_key):
        logger.info("Handoff decision not notified: cooldown active for session=%s", notification_key)


@app.get("/health")
async def health():
    return {"status": "ok", "provider": settings.AI_PROVIDER, "model": settings.AI_MODEL}


@app.get("/webhook")
async def verify_webhook(request: Request):
    if not is_meta_channel(_runtime_channel()):
        raise HTTPException(status_code=404, detail="Webhook is not enabled for this channel")
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == meta_verify_token():
        return PlainTextResponse(challenge or "")
    raise HTTPException(status_code=403, detail="Invalid verify token")


@app.post("/webhook")
async def receive_message(request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()
    if not verify_meta_signature(raw_body, request.headers.get("X-Hub-Signature-256")):
        logger.warning("Meta webhook rejected: invalid signature")
        raise HTTPException(status_code=403, detail="Invalid signature")
    try:
        body = json.loads(raw_body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"status": "invalid_json"}, status_code=400)
    logger.info("Incoming webhook object=%s", body.get("object") if isinstance(body, dict) else None)
    try:
        runtime_channel = _runtime_channel()
        if not is_meta_channel(runtime_channel):
            return JSONResponse({"status": "ignored_channel"})

        parse_status, incoming = parse_incoming_message(runtime_channel, body)
        if incoming is None:
            logger.info("Webhook ignored for channel=%s status=%s", channel_label(runtime_channel), parse_status)
            return JSONResponse({"status": parse_status})

        if processed_store.is_processed(incoming.message_id):
            logger.info("Duplicate message ignored: %s", incoming.message_id)
            return JSONResponse({"status": "duplicate_ignored"})

        if not _within_limits(f"{runtime_channel}:{incoming.sender_id}", incoming.text):
            processed_store.mark_processed(incoming.message_id)
            return JSONResponse({"status": "rate_limited"})

        processed_store.mark_processed(incoming.message_id)
        background_tasks.add_task(_process_message, runtime_channel, incoming.sender_id, incoming.text)
        return JSONResponse({"status": "ok"})

    except Exception:
        logger.exception("Error processing webhook")
        return JSONResponse({"status": "error"})


@app.post("/chat")
async def web_chat(payload: WebChatRequest):
    # Only bots generated for the web channel expose a public chat endpoint.
    # Otherwise this endpoint is a free, unauthenticated door to the AI key.
    if settings.WEB_CHAT_REQUIRES_WEB_CHANNEL and _runtime_channel() != "web":
        raise HTTPException(status_code=404, detail="Web chat is not enabled for this bot")

    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    session_id = _normalize_web_session_id(payload.session_id)
    session_key = _session_key("web", session_id)
    if not _within_limits(session_key, message):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down and try again shortly.")
    response_text, updated_history, current_config, decision = await _handle_conversation_turn(
        session_key,
        message,
        prompt_context=session_id,
    )
    await _maybe_send_handoff_email(
        notification_key=session_key,
        contact_value=session_id,
        contact_label="Web session",
        current_config=current_config,
        user_text=message,
        response_text=response_text,
        updated_history=updated_history,
        decision=decision,
    )
    return JSONResponse(
        {
            "session_id": session_id,
            "message": response_text,
            "handoff": {
                "triggered": bool(decision),
                "reason": decision.reason if decision else "",
                "matched_value": decision.matched_value if decision else "",
            },
        }
    )


@app.post("/telegram-webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TELEGRAM_WEBHOOK_SECRET:
        logger.warning("Telegram webhook rejected: invalid secret token")
        raise HTTPException(status_code=403, detail="Invalid secret token")

    body = await request.json()
    logger.info("Incoming Telegram update_id=%s", body.get("update_id") if isinstance(body, dict) else None)
    try:
        parse_status, incoming = parse_incoming_message("telegram", body)
        if incoming is None:
            logger.info("Telegram webhook ignored: status=%s", parse_status)
            return JSONResponse({"status": parse_status})

        if processed_store.is_processed(incoming.message_id):
            logger.info("Duplicate Telegram update ignored: %s", incoming.message_id)
            return JSONResponse({"status": "duplicate_ignored"})

        if not _within_limits(f"telegram:{incoming.sender_id}", incoming.text):
            processed_store.mark_processed(incoming.message_id)
            return JSONResponse({"status": "rate_limited"})

        processed_store.mark_processed(incoming.message_id)
        background_tasks.add_task(_process_message, "telegram", incoming.sender_id, incoming.text)
        return JSONResponse({"status": "ok"})

    except Exception:
        logger.exception("Error processing Telegram webhook")
        return JSONResponse({"status": "error"})


async def _process_message(channel: str, recipient_id: str, user_text: str):
    try:
        normalized_channel = normalize_channel(channel)
        session_key = _session_key(normalized_channel, recipient_id)
        response_text, updated_history, current_config, decision = await _handle_conversation_turn(
            session_key,
            user_text,
            prompt_context=recipient_id,
        )
        await send_channel_message(normalized_channel, recipient_id, response_text)
        await _maybe_send_handoff_email(
            notification_key=session_key,
            contact_value=recipient_id,
            contact_label=customer_contact_label(normalized_channel),
            current_config=current_config,
            user_text=user_text,
            response_text=response_text,
            updated_history=updated_history,
            decision=decision,
        )
    except Exception:
        logger.exception("Error in background task for channel=%s recipient=%s", channel_label(channel), recipient_id)
