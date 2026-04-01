import logging
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse

from app.core.ai_engine import AIEngine
from app.core.config import settings
from app.core.session import SessionStore, ProcessedMessageStore
from app.core.whatsapp import send_whatsapp_message
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

ai_engine_instance = AIEngine()
session_store = SessionStore()
processed_store = ProcessedMessageStore()

import app.core.ai_engine as _ae_module
_ae_module.ai_engine_instance = ai_engine_instance

app.include_router(admin_router.router)


@app.get("/health")
async def health():
    return {"status": "ok", "provider": settings.AI_PROVIDER, "model": settings.AI_MODEL}


@app.get("/webhook")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == settings.WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(challenge or "")
    raise HTTPException(status_code=403, detail="Invalid verify token")


@app.post("/webhook")
async def receive_message(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    logger.info("Incoming webhook: %s", body)
    try:
        entry = (body.get("entry") or [])[0]
        change = (entry.get("changes") or [])[0]
        value = change.get("value") or {}

        if "statuses" in value:
            return JSONResponse({"status": "ignored_status"})

        messages = value.get("messages") or []
        if not messages:
            return JSONResponse({"status": "no_message"})

        message = messages[0]
        message_type = message.get("type")
        if message_type != "text":
            return JSONResponse({"status": f"ignored_{message_type or 'unknown'}"})

        message_id = message.get("id")
        wa_id = message.get("from")
        user_text = ((message.get("text") or {}).get("body") or "").strip()

        if not message_id or not wa_id or not user_text:
            return JSONResponse({"status": "invalid_message"})

        if processed_store.is_processed(message_id):
            logger.info("Duplicate message ignored: %s", message_id)
            return JSONResponse({"status": "duplicate_ignored"})

        processed_store.mark_processed(message_id)
        background_tasks.add_task(_process_message, wa_id, user_text)
        return JSONResponse({"status": "ok"})

    except Exception:
        logger.exception("Error processing webhook")
        return JSONResponse({"status": "error"})


async def _process_message(wa_id: str, user_text: str):
    try:
        history = session_store.get(wa_id)
        response_text, updated_history = await ai_engine_instance.process(
            user_message=user_text, history=history, phone_number=wa_id,
        )
        if updated_history is not None:
            session_store.set(wa_id, updated_history)
        await send_whatsapp_message(wa_id, response_text)
    except Exception:
        logger.exception("Error in background task for wa_id=%s", wa_id)
