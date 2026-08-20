"""Local AI provider configuration for the Email Automation LangGraph agent.

Email Automation owns its own `email_ai_config` only. TACWork's AI provider is
configured entirely inside the TACWork web UI and is intentionally NOT touched
here (no shared flag, no opencode.jsonc provider injection).

Backward-compat: a legacy `unified_ai_config` flag (and the earlier
`tacwork_ai_config` split flag) are migrated/cleaned into `email_ai_config`
on first read and then deleted.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..security import get_cipher

EMAIL_FLAG = "email_ai_config"
OLD_FLAG = "unified_ai_config"
TACWORK_FLAG = "tacwork_ai_config"  # legacy split flag, only used for cleanup


@dataclass
class EmailAIConfig:
    provider_name: str
    base_url: str
    model: str
    api_key: str


def _read_flag(db: Session, key: str) -> dict | None:
    row = db.get(models.SystemFlag, key)
    if not row or not row.value:
        return None
    try:
        return json.loads(get_cipher().decrypt(row.value) or "{}")
    except Exception:
        return None


def _write_flag(db: Session, key: str, value: dict) -> None:
    encrypted = get_cipher().encrypt(json.dumps(value, ensure_ascii=False))
    row = db.get(models.SystemFlag, key)
    if row:
        row.value = encrypted
    else:
        db.add(models.SystemFlag(key=key, value=encrypted))


def _delete_flag(db: Session, key: str) -> None:
    row = db.get(models.SystemFlag, key)
    if row:
        db.delete(row)


def _migrate_legacy(db: Session) -> None:
    """Migrate a legacy unified flag into `email_ai_config` once, then delete it."""
    legacy = _read_flag(db, OLD_FLAG)
    if legacy is None:
        # also clean up any leftover tacwork flag from the earlier split
        _delete_flag(db, TACWORK_FLAG)
        return
    email = {
        "provider_name": legacy.get("provider_name", "openai-compatible"),
        "base_url": legacy.get("base_url") or "https://api.openai.com/v1",
        "model": legacy.get("model") or "",
        "api_key": legacy.get("api_key") or "",
    }
    _write_flag(db, EMAIL_FLAG, email)
    _delete_flag(db, OLD_FLAG)
    _delete_flag(db, TACWORK_FLAG)
    db.commit()


def load_email(db: Session) -> EmailAIConfig:
    _migrate_legacy(db)
    raw = _read_flag(db, EMAIL_FLAG)
    if raw and raw.get("api_key") and raw.get("model"):
        return EmailAIConfig(**raw)
    settings = get_settings()
    return EmailAIConfig(
        provider_name=settings.LLM_PROVIDER or "openai-compatible",
        base_url=settings.LLM_BASE_URL or "https://api.openai.com/v1",
        model=settings.effective_llm_model,
        api_key=settings.effective_llm_api_key or "",
    )


def peek_email_config(db: Session) -> Optional[EmailAIConfig]:
    """Read the DB-backed email AI config WITHOUT running legacy migration.

    Safe for hot/read paths (health checks, per-call LLM construction): it only
    decrypts and never writes. Returns None when the flag is absent or lacks a
    usable api_key/model.
    """
    raw = _read_flag(db, EMAIL_FLAG)
    if raw and raw.get("api_key") and raw.get("model"):
        return EmailAIConfig(**raw)
    return None


def save_email(db: Session, config: EmailAIConfig) -> None:
    _write_flag(db, EMAIL_FLAG, asdict(config))


def public_email(config: EmailAIConfig) -> dict:
    return {
        "provider_name": config.provider_name,
        "base_url": config.base_url,
        "model": config.model,
        "api_key_configured": bool(config.api_key),
        "restart_required": True,
    }
