"""Owner-level Agent Profile API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..services.agent_profile import get_or_create_profile, validate_profile_signature
from ..services import ai_config as ai_config_svc
from .deps import ensure_owner, get_db

router = APIRouter(prefix="/api/agent-profile", tags=["agent-profile"])


class AgentProfileUpdate(BaseModel):
    agent_name: str = Field(min_length=1, max_length=200)
    company_name: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=300)
    tone: str = Field(min_length=1, max_length=100)
    language_policy: str = Field(pattern="^(match_customer|chinese|english)$")
    signature_text: str = Field(min_length=1, max_length=2_000)
    forbidden_claims: str = Field(default="", max_length=5_000)
    unknown_answer_policy: str = Field(min_length=1, max_length=5_000)
    allow_campaign_override: bool = False
    approval_mode: str | None = Field(default=None, pattern="^(human_review|agent_review)$")
    is_active: bool = True


class ApprovalModeUpdate(BaseModel):
    approval_mode: str = Field(pattern="^(human_review|agent_review)$")


class EmailAIConfigUpdate(BaseModel):
    provider_name: str = Field(default="openai-compatible", min_length=1, max_length=100)
    base_url: str = Field(min_length=8, max_length=500)
    model: str = Field(min_length=1, max_length=300)
    api_key: str | None = Field(default=None, max_length=2_000)


def _out(row):
    return {
        "id": row.id,
        "agent_name": row.agent_name,
        "company_name": row.company_name,
        "role": row.role,
        "tone": row.tone,
        "language_policy": row.language_policy,
        "signature_text": row.signature_text,
        "forbidden_claims": row.forbidden_claims,
        "unknown_answer_policy": row.unknown_answer_policy,
        "allow_campaign_override": row.allow_campaign_override,
        "approval_mode": row.approval_mode or "human_review",
        "is_active": row.is_active,
        "version": row.version,
        "updated_at": row.updated_at,
    }


@router.get("")
def get_profile(
    create_if_missing: bool = Query(True),
    db: Session = Depends(get_db),
):
    owner_id = ensure_owner(db)
    if not create_if_missing:
        row = db.query(models.AgentProfile).filter_by(owner_id=owner_id).first()
        return {"configured": False} if row is None else {"configured": True, **_out(row)}
    row = get_or_create_profile(db, owner_id)
    db.commit()
    db.refresh(row)
    return _out(row)


@router.put("")
def update_profile(payload: AgentProfileUpdate, actor: str = "user", db: Session = Depends(get_db)):
    try:
        validate_profile_signature(payload.agent_name, payload.company_name, payload.signature_text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    owner_id = ensure_owner(db)
    row = get_or_create_profile(db, owner_id)
    for key, value in payload.model_dump().items():
        if value is None:
            continue
        setattr(row, key, value.strip() if isinstance(value, str) else value)
    row.version += 1
    db.add(models.AuditLog(
        actor=actor,
        action="agent_profile_updated",
        entity="agent_profile",
        entity_id=str(row.id),
        detail=f"version={row.version}; agent={row.agent_name}; company={row.company_name}",
    ))
    db.commit()
    db.refresh(row)
    return _out(row)


@router.put("/approval-mode")
def update_approval_mode(payload: ApprovalModeUpdate, actor: str = "user", db: Session = Depends(get_db)):
    """Set the Workspace-wide maximum authority for Agent-originated sends."""
    owner_id = ensure_owner(db)
    row = get_or_create_profile(db, owner_id)
    old_mode = row.approval_mode or "human_review"
    row.approval_mode = payload.approval_mode
    row.version += 1
    db.add(models.AuditLog(
        actor=actor,
        action="agent_approval_mode_updated",
        entity="agent_profile",
        entity_id=str(row.id),
        detail=f"approval_mode={old_mode}->{row.approval_mode}",
    ))
    db.commit()
    db.refresh(row)
    return _out(row)


def _safe_provider_url(value: str) -> str:
    from urllib.parse import urlparse
    url = value.strip().rstrip("/")
    parsed = urlparse(url)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise HTTPException(status_code=422, detail="provider_url_must_be_https_or_loopback")
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(status_code=422, detail="invalid_provider_url")
    return url


def _build_email_config(payload: EmailAIConfigUpdate, db: Session) -> "ai_config_svc.EmailAIConfig":
    current, state = ai_config_svc.email_config_state(db, migrate_legacy=True)
    if state.get("source") != "db" and not (payload.api_key or "").strip():
        raise HTTPException(status_code=422, detail="api_key_required_for_new_or_unreadable_credential")
    key = (payload.api_key or "").strip() or current.api_key
    if not key:
        raise HTTPException(status_code=422, detail="api_key_required")
    base_url = _safe_provider_url(payload.base_url)
    return ai_config_svc.EmailAIConfig(
        provider_name=payload.provider_name.strip(),
        base_url=base_url,
        model=payload.model.strip(),
        api_key=key,
    )


@router.get("/email-ai-config")
def get_email_ai_config(db: Session = Depends(get_db)):
    config, state = ai_config_svc.email_config_state(db, migrate_legacy=True)
    return ai_config_svc.public_email(config, state)


@router.post("/email-ai-config/test")
def test_email_ai_config(payload: EmailAIConfigUpdate, db: Session = Depends(get_db)):
    cfg = _build_email_config(payload, db)
    try:
        import httpx
        response = httpx.post(
            f"{cfg.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            json={"model": cfg.model, "messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 8, "temperature": 0},
            timeout=20,
        )
        if response.status_code >= 400:
            raise HTTPException(status_code=409, detail=f"provider_test_failed:{response.status_code}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"provider_test_failed:{type(exc).__name__}") from exc
    return {"ok": True, "provider": cfg.provider_name, "model": cfg.model, "target": cfg.base_url}


@router.put("/email-ai-config")
def update_email_ai_config(payload: EmailAIConfigUpdate, db: Session = Depends(get_db)):
    cfg = _build_email_config(payload, db)
    ai_config_svc.save_email(db, cfg)
    db.add(models.AuditLog(actor="user", action="email_ai_config_updated", entity="system_config", detail=f"provider={cfg.provider_name}; model={cfg.model}"))
    db.commit()
    return ai_config_svc.public_email(cfg, {
        "source": "db", "db_config_present": True, "db_config_readable": True,
        "usable": True, "error_code": None,
    })
