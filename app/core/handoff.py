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
