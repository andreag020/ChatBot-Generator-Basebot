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
