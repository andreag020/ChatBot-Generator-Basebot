"""
Admin endpoints for runtime bot configuration management.

POST  /admin/bot-config          — update bot_config.yaml
GET   /admin/bot-config          — read current config
POST  /admin/bot-config/rollback — restore last backup
GET   /admin/bot-config/history  — list available backups

Security: all endpoints require X-Admin-Token header.
Set ADMIN_TOKEN in .env (min 32 chars recommended).
"""

import asyncio
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

_write_lock = asyncio.Lock()
BACKUP_DIR = Path("config/backups")
MAX_BACKUPS = 10
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class BusinessConfig(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    bot_name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=4000)
    phone: str = Field(default="", max_length=50)
    whatsapp: str = Field(default="", max_length=50)
    email: str = Field(default="", max_length=200)
    website: str = Field(default="", max_length=200)
    hours: str = Field(default="", max_length=200)
    address: str = Field(default="", max_length=300)


class ToneConfig(BaseModel):
    personality: str = Field(default="profesional", max_length=200)
    language: str = Field(default="espanol formal", max_length=100)
    use_emojis: bool = False
    response_style: str = Field(default="", max_length=1000)


class ServiceConfig(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    sectors: list[str] = Field(default_factory=list)
    modalities: list[str] = Field(default_factory=list)


class FAQItem(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=4000)


class ObjectionGuide(BaseModel):
    trigger: str = Field(min_length=1, max_length=200)
    response: str = Field(min_length=1, max_length=1000)


class HandoffConfig(BaseModel):
    enabled: bool = True
    notify_team: bool = False
    message: str = Field(default="", max_length=1000)
    triggers: list[str] = Field(default_factory=list)
    notification_emails: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("notification_emails", mode="before")
    @classmethod
    def normalize_notification_emails(cls, v):
        if not isinstance(v, list):
            return []

        emails: list[str] = []
        invalid: list[str] = []
        for item in v:
            email = str(item or "").strip().lower()
            if not email:
                continue
            if not EMAIL_PATTERN.match(email):
                invalid.append(email)
                continue
            if email not in emails:
                emails.append(email)

        if invalid:
            raise ValueError(f"Invalid notification email(s): {', '.join(invalid)}")
        return emails


class FallbackConfig(BaseModel):
    unknown_answer: str = Field(default="", max_length=1000)
    out_of_scope: str = Field(default="", max_length=1000)


class BotConfigPayload(BaseModel):
    business: BusinessConfig
    tone: ToneConfig = Field(default_factory=ToneConfig)
    objectives: list[str] = Field(default_factory=list, max_length=20)
    lead_fields: list[str] = Field(default_factory=list, max_length=10)
    services: list[ServiceConfig] = Field(default_factory=list, max_length=30)
    certifications: list[str] = Field(default_factory=list, max_length=20)
    faqs: list[FAQItem] = Field(default_factory=list, max_length=50)
    objection_guides: list[ObjectionGuide] = Field(default_factory=list, max_length=20)
    handoff: HandoffConfig = Field(default_factory=HandoffConfig)
    fallback: FallbackConfig = Field(default_factory=FallbackConfig)
    rules: list[str] = Field(default_factory=list, max_length=30)

    @field_validator(
        "rules",
        "objectives",
        "certifications",
        "lead_fields",
        mode="before",
    )
    @classmethod
    def strip_empty_strings(cls, v):
        if isinstance(v, list):
            return [item.strip() for item in v if isinstance(item, str) and item.strip()]
        return v

    @field_validator("handoff", mode="before")
    @classmethod
    def normalize_handoff(cls, v):
        if isinstance(v, dict):
            v["triggers"] = [item.strip() for item in v.get("triggers", []) if isinstance(item, str) and item.strip()]
            v["notification_emails"] = [
                str(item).strip()
                for item in v.get("notification_emails", [])
                if str(item).strip()
            ]
        return v


def verify_admin_token(x_admin_token: str = Header(..., alias="X-Admin-Token")):
    expected = settings.ADMIN_TOKEN
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ADMIN_TOKEN not configured on server",
        )
    if x_admin_token != expected:
        logger.warning("Invalid admin token attempt")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin token",
        )
    return True


def _config_path() -> Path:
    return Path(settings.CONFIG_PATH)


def _make_backup(config_path: Path) -> Path | None:
    if not config_path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup = BACKUP_DIR / f"bot_config_{ts}.yaml"
    shutil.copy2(config_path, backup)
    _prune_old_backups()
    logger.info("Backup saved: %s", backup)
    return backup


def _prune_old_backups():
    backups = sorted(BACKUP_DIR.glob("bot_config_*.yaml"), reverse=True)
    for old in backups[MAX_BACKUPS:]:
        old.unlink(missing_ok=True)
        logger.info("Pruned old backup: %s", old)


def _write_yaml_atomic(path: Path, data: dict):
    tmp = path.with_suffix(".yaml.tmp")
    yaml_str = yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    tmp.write_text(yaml_str, encoding="utf-8")
    tmp.replace(path)


@router.post("/bot-config", dependencies=[Depends(verify_admin_token)])
async def update_bot_config(payload: BotConfigPayload):
    config_path = _config_path()

    async with _write_lock:
        backup_path = _make_backup(config_path)

        try:
            data = payload.model_dump(exclude_none=False)
            _write_yaml_atomic(config_path, data)
            logger.info("bot_config.yaml updated successfully")
        except Exception as exc:
            logger.error("Failed to write config: %s", exc)
            if backup_path and backup_path.exists():
                shutil.copy2(backup_path, config_path)
                logger.info("Auto-restored from backup after write failure")
            raise HTTPException(status_code=500, detail=f"Config write failed: {exc}")

    try:
        from app.core.ai_engine import ai_engine_instance
        if ai_engine_instance:
            reloaded = ai_engine_instance.prompt_builder.force_reload()
            logger.info("PromptBuilder force_reload: changed=%s", reloaded)
    except Exception as exc:
        logger.warning("Could not trigger PromptBuilder reload: %s", exc)

    return {
        "status": "updated",
        "backup": str(backup_path) if backup_path else None,
        "message": "Bot configuration updated. Changes active on next message.",
    }


@router.get("/bot-config", dependencies=[Depends(verify_admin_token)])
async def get_bot_config():
    config_path = _config_path()
    if not config_path.exists():
        raise HTTPException(status_code=404, detail="Config file not found")
    try:
        raw = config_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
        return data
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not read config: {exc}")


@router.post("/bot-config/rollback", dependencies=[Depends(verify_admin_token)])
async def rollback_bot_config(backup_filename: str | None = None):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backups = sorted(BACKUP_DIR.glob("bot_config_*.yaml"), reverse=True)

    if not backups:
        raise HTTPException(status_code=404, detail="No backups available")

    if backup_filename:
        target = BACKUP_DIR / backup_filename
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"Backup not found: {backup_filename}")
    else:
        target = backups[0]

    async with _write_lock:
        _make_backup(_config_path())
        shutil.copy2(target, _config_path())
        logger.info("Rolled back to: %s", target)

    try:
        from app.core.ai_engine import ai_engine_instance
        if ai_engine_instance:
            ai_engine_instance.prompt_builder.force_reload()
    except Exception:
        pass

    return {"status": "rolled_back", "restored_from": target.name}


@router.get("/bot-config/history", dependencies=[Depends(verify_admin_token)])
async def config_history():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backups = sorted(BACKUP_DIR.glob("bot_config_*.yaml"), reverse=True)
    return {
        "backups": [
            {"filename": b.name, "size_bytes": b.stat().st_size}
            for b in backups
        ]
    }
