"""Agent comparison endpoints + adoption metrics."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..schemas import ComparisonSelection
from .deps import get_db

router = APIRouter(prefix="/api/comparisons", tags=["comparisons"])


@router.get("")
def list_comparisons(db: Session = Depends(get_db), limit: int = 50):
    rows = db.query(models.AgentComparison).order_by(models.AgentComparison.created_at.desc()).limit(limit).all()
    return [
        {
            "comparison_key": c.comparison_key, "task_type": c.task_type,
            "intent_agree": c.intent_agree, "action_agree": c.action_agree,
            "selected": c.selected, "adopted": c.adopted,
            "langgraph_run_id": c.langgraph_run_id, "openclaw_run_id": c.openclaw_run_id,
            "created_at": c.created_at,
        }
        for c in rows
    ]


@router.get("/{key}")
def get_comparison(key: str, db: Session = Depends(get_db)):
    c = db.query(models.AgentComparison).filter_by(comparison_key=key).first()
    if not c:
        raise HTTPException(status_code=404, detail="comparison not found")
    lg = db.query(models.AgentRun).filter_by(run_id=c.langgraph_run_id).first() if c.langgraph_run_id else None
    oc = db.query(models.AgentRun).filter_by(run_id=c.openclaw_run_id).first() if c.openclaw_run_id else None
    return {
        "comparison_key": c.comparison_key, "task_type": c.task_type, "thread_id": c.thread_id,
        "intent_agree": c.intent_agree, "action_agree": c.action_agree,
        "selected": c.selected, "adopted": c.adopted,
        "langgraph": _run_to_decision(lg), "openclaw": _run_to_decision(oc),
    }


def _run_to_decision(run):
    if not run:
        return None
    import json
    base = {
        "agent": run.agent, "run_id": run.run_id, "intent": run.intent,
        "confidence": (run.confidence or 0) / 100.0, "summary": run.summary,
        "recommended_action": run.recommended_action, "reasoning_summary": run.reasoning_summary,
        "risk_level": run.risk_level, "requires_approval": run.requires_approval,
        "model": run.model, "latency_ms": run.latency_ms, "prompt_version": run.prompt_version,
    }
    return base


@router.post("/{key}/select")
def select(key: str, payload: ComparisonSelection, db: Session = Depends(get_db)):
    c = db.query(models.AgentComparison).filter_by(comparison_key=key).first()
    if not c:
        raise HTTPException(status_code=404, detail="comparison not found")
    c.selected = payload.selected
    c.adopted = payload.selected in ("langgraph", "openclaw", "edited")
    c.notes = payload.notes
    db.commit()
    return {"ok": True, "selected": c.selected, "adopted": c.adopted}


@router.get("/metrics/summary")
def comparison_metrics(db: Session = Depends(get_db)):
    rows = db.query(models.AgentComparison).all()
    total = len(rows)
    if total == 0:
        return {"total": 0, "adoption": {}, "avg_latency": {}, "failure_rate": {}, "intent_disagreement": None}
    adopted_lg = sum(1 for r in rows if r.selected == "langgraph")
    adopted_oc = sum(1 for r in rows if r.selected == "openclaw")
    adopted_edited = sum(1 for r in rows if r.selected == "edited")
    none = sum(1 for r in rows if r.selected == "none")
    disagree = sum(1 for r in rows if r.intent_agree is False)
    # failure: openclaw run missing or failed
    oc_failed = 0
    for r in rows:
        oc = db.query(models.AgentRun).filter_by(run_id=r.openclaw_run_id).first() if r.openclaw_run_id else None
        if oc is None or oc.status == "failed":
            oc_failed += 1
    lg_runs = db.query(models.AgentRun).filter_by(agent="langgraph").all()
    oc_runs = db.query(models.AgentRun).filter_by(agent="openclaw").all()
    avg_lat = lambda runs: round(sum(x.latency_ms or 0 for x in runs) / len(runs), 1) if runs else 0
    return {
        "total": total,
        "adoption": {"langgraph": adopted_lg, "openclaw": adopted_oc, "edited": adopted_edited, "none": none},
        "avg_latency_ms": {"langgraph": avg_lat(lg_runs), "openclaw": avg_lat(oc_runs)},
        "openclaw_failure_rate": round(oc_failed / total, 3),
        "intent_disagreement_rate": round(disagree / total, 3),
    }
