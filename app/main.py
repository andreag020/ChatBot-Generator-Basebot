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
)
from app.core.config import settings
from app.core.handoff import evaluate_handoff, handoff_notifications_enabled, send_handoff_notification
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
        
        # Fix padding if needed
        padding = len(payload_b64) % 4
        if padding > 0:
            payload_b64 += "=" * (4 - padding)
            
        decoded_json_str = base64.b64decode(payload_b64).decode("utf-8")
        payload = json.loads(unquote(decoded_json_str)) if "%" in decoded_json_str else json.loads(decoded_json_str)
        
        deploy_config = payload.pop("deploy", {})
        payload.pop("locale", None)
        
        # Override dynamic settings
        if "provider" in deploy_config:
            settings.AI_PROVIDER = deploy_config["provider"]
            logger.info("[BOOTSTRAP] AI_PROVIDER set to: %s", settings.AI_PROVIDER)
        if "model" in deploy_config:
            # Auto-upgrade deprecated model names
            DEPRECATED_MODELS = {
                "claude-3-haiku-20240307": "claude-haiku-4-5-20251001",
                "claude-3-5-haiku-20241022": "claude-haiku-4-5-20251001",
            }
            model_name = deploy_config["model"]
            if model_name in DEPRECATED_MODELS:
                new_model = DEPRECATED_MODELS[model_name]
                logger.warning(
                    "[BOOTSTRAP] Deprecated model '%s' auto-upgraded to '%s'",
                    model_name, new_model,
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

        runtime = dict(payload.get("runtime") or {})
        if deploy_config.get("channel"):
            runtime["channel"] = deploy_config["channel"]
        if runtime:
            payload["runtime"] = runtime
            
        # Write the YAML configuration
        os.makedirs("config", exist_ok=True)
        with open("config/bot_config.yaml", "w", encoding="utf-8") as f:
            yaml.dump(payload, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            
        logger.info("Successfully applied BOT_PAYLOAD_B64 to memory and bot_config.yaml")
        logger.info("[BOOTSTRAP] Final effective settings -> AI_PROVIDER=%s | AI_MODEL=%s | ANTHROPIC_KEY_SET=%s",
                    settings.AI_PROVIDER, settings.AI_MODEL, bool(settings.ANTHROPIC_API_KEY))
    except Exception as e:
        logger.error(f"Failed to bootstrap BOT_PAYLOAD_B64: {e}")
# ---------------------------------------------------------

logger.info("[STARTUP] Initializing AIEngine with provider=%s model=%s", settings.AI_PROVIDER, settings.AI_MODEL)
ai_engine_instance = AIEngine()
logger.info("[STARTUP] AIEngine ready. engine.provider=%s | client_set=%s", ai_engine_instance.provider, ai_engine_instance.anthropic_client is not None)
session_store = SessionStore()
processed_store = ProcessedMessageStore()
handoff_notification_store = HandoffNotificationStore()

import app.core.ai_engine as _ae_module
_ae_module.ai_engine_instance = ai_engine_instance

app.include_router(admin_router.router)


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


async def _handle_conversation_turn(session_key: str, user_text: str, *, prompt_context: str) -> tuple[str, list[dict], dict, object | None]:
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
    else:
        logger.info("No handoff decision created for session=%s", session_key)
    return response_text, updated_history, current_config, decision


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
        handoff_notification_store.mark_sent(notification_key)
        await send_handoff_notification(
            current_config,
            phone_number=contact_value,
            contact_label=contact_label,
            user_message=user_text,
            response_text=response_text,
            history=updated_history,
            decision=decision,
        )
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
    body = await request.json()
    logger.info("Incoming webhook: %s", body)
    try:
        runtime_channel = _runtime_channel()
        if not is_meta_channel(runtime_channel):
            return JSONResponse({"status": "ignored_channel"})

        parse_status, incoming = parse_incoming_message(runtime_channel, body)
        if incoming is None:
            return JSONResponse({"status": parse_status})

        if processed_store.is_processed(incoming.message_id):
            logger.info("Duplicate message ignored: %s", incoming.message_id)
            return JSONResponse({"status": "duplicate_ignored"})

        processed_store.mark_processed(incoming.message_id)
        background_tasks.add_task(_process_message, runtime_channel, incoming.sender_id, incoming.text)
        return JSONResponse({"status": "ok"})

    except Exception:
        logger.exception("Error processing webhook")
        return JSONResponse({"status": "error"})


@app.post("/chat")
async def web_chat(payload: WebChatRequest):
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    session_id = _normalize_web_session_id(payload.session_id)
    session_key = _session_key("web", session_id)
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
