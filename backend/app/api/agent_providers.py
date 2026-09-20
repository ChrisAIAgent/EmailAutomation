"""Workspace Agent Provider discovery and selection."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..services import agent_providers as provider_svc
from ..services import agent_takeover as takeover_svc
from ..services import flags
from .deps import ensure_owner, get_db

router = APIRouter(tags=["agent-providers"])


class AgentProviderSelection(BaseModel):
    provider_id: str = Field(min_length=1, max_length=60)


def _status_code(error: provider_svc.AgentProviderError) -> int:
    if error.code in {"agent_provider_not_configured", "agent_provider_capability_missing", "agent_provider_session_active"}:
        return 409
    if error.code in {"agent_provider_unreachable", "agent_provider_timeout"}:
        return 503
    return 502


def _out(db: Session) -> dict:
    selected = provider_svc.selected_provider_id(db)
    providers = provider_svc.provider_registry()
    return {
        "selected_provider": selected,
        "providers": [
            provider_svc.public_provider(provider, include_surface=provider.id == selected)
            for provider in providers.values()
        ],
    }


@router.get("/api/agent-providers")
def list_agent_providers(db: Session = Depends(get_db)):
    ensure_owner(db)
    return _out(db)


@router.put("/api/agent-provider")
def select_agent_provider(body: AgentProviderSelection, db: Session = Depends(get_db)):
    ensure_owner(db)
    requested_id = body.provider_id.strip().lower()
    current_id = provider_svc.selected_provider_id(db)
    if requested_id == current_id:
        return _out(db)
    try:
        requested = provider_svc.get_provider(requested_id)
        provider_svc.require_capability(requested, provider_svc.CAP_INTERACTIVE_SESSION)
        requested.health()
        current = provider_svc.get_provider(current_id)
        takeover = takeover_svc.status(db)
        if takeover.get("active_session_id") or current.has_active_session():
            raise provider_svc.AgentProviderError("agent_provider_session_active", current_id)
    except provider_svc.AgentProviderError as exc:
        raise HTTPException(status_code=_status_code(exc), detail=exc.code) from exc

    flags.set_flag(db, provider_svc.SELECTED_PROVIDER_FLAG, requested_id)
    db.add(models.AuditLog(
        actor="user",
        action="agent_provider_changed",
        entity="agent_provider",
        entity_id=requested_id,
        detail=json.dumps({"from": current_id, "to": requested_id}),
    ))
    db.commit()
    return _out(db)
