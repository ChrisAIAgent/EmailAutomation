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


def _read_flag_with_state(db: Session, key: str) -> tuple[dict | None, bool, str | None]:
    row = db.get(models.SystemFlag, key)
    if not row or not row.value:
        return None, False, None
    try:
        decrypted = get_cipher().decrypt(row.value)
        if not decrypted:
            return None, False, "credential_unreadable"
        value = json.loads(decrypted)
        if not isinstance(value, dict):
            return None, False, "credential_unreadable"
        return value, True, None
    except Exception:
        # A value encrypted by another runtime/key is not a configured
        # credential here. Never expose or attempt to recover it.
        return None, False, "credential_unreadable"


def _read_flag(db: Session, key: str) -> dict | None:
    return _read_flag_with_state(db, key)[0]


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
    config, _state = email_config_state(db, migrate_legacy=True)
    return config


def email_config_state(db: Session, *, migrate_legacy: bool = False) -> tuple[EmailAIConfig, dict]:
    """Resolve the effective config and an API-safe source/readability state."""
    if migrate_legacy:
        _migrate_legacy(db)
    raw, readable, error_code = _read_flag_with_state(db, EMAIL_FLAG)
    db_present = db.get(models.SystemFlag, EMAIL_FLAG) is not None
    if raw and raw.get("api_key") and raw.get("model"):
        return EmailAIConfig(**raw), {
            "source": "db", "db_config_present": db_present,
            "db_config_readable": True, "usable": True, "error_code": None,
        }
    settings = get_settings()
    config = EmailAIConfig(
        provider_name=settings.LLM_PROVIDER or "openai-compatible",
        base_url=settings.LLM_BASE_URL or "https://api.openai.com/v1",
        model=settings.effective_llm_model,
        api_key=settings.effective_llm_api_key or "",
    )
    usable = bool(config.api_key and config.model)
    return config, {
        "source": "env" if usable else "none",
        "db_config_present": db_present,
        "db_config_readable": readable if db_present else None,
        "usable": usable,
        "error_code": error_code or ("credential_incomplete" if db_present and readable else None),
    }


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


def public_email(config: EmailAIConfig, state: dict | None = None) -> dict:
    return {
        "provider_name": config.provider_name,
        "base_url": config.base_url,
        "model": config.model,
        "api_key_configured": bool(config.api_key),
        # This DB-backed configuration is resolved for every new LangGraph
        # operation. Claiming a restart was required left the UI stale after a
        # successful encrypted save.
        "restart_required": False,
        "applies_to_next_run": True,
        **(state or {
            "source": "db", "db_config_present": True, "db_config_readable": True,
            "usable": bool(config.api_key and config.model), "error_code": None,
        }),
    }
