"""FastAPI application entrypoint."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import get_settings, is_gmail_configured, is_llm_configured, _DATA
from .services.ai_config import email_config_state
from .consumer_status import read_consumer_status
from .db import init_db, SessionLocal
from .events import queue as event_queue
from . import models  # noqa: F401  (register models)
from .logging_config import configure_logging, trace_id_var
from .api import gmail, campaigns, contacts, inbox, approvals, comparisons, dashboard, system, automation, agent_runs, knowledge, agent_profile, agent_takeover
from .api.deps import ensure_owner, get_db
from .errors import register_error_handlers
from .scheduler import start as start_scheduler
from .services import flags as flag_svc
from .services.accounts import resolve_sending_account
from .services.real_send import is_real_send_enabled

configure_logging()
logger = logging.getLogger("main")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Apply additive column migrations so a stale app.db gains any new columns
    # without a manual `python migrate_db.py` step. Failures are logged but do
    # not block startup (create_all already ensured all tables exist).
    try:
        from .migrations import run as run_migrations
        run_migrations()
    except Exception as exc:
        logger.error("startup migration failed: %s", exc)
    # ensure a default owner exists
    db = SessionLocal()
    try:
        from .api.deps import ensure_owner
        owner_id = ensure_owner(db)
        from .services.sync import repair_stored_mail_bodies
        repaired_bodies = repair_stored_mail_bodies(db)
        if repaired_bodies:
            logger.info("Repaired %s locally stored HTML/CSS email bodies.", repaired_bodies)
        from .services.inbox_triage import reconcile_existing_contact_reviews
        reconciled_reviews = reconcile_existing_contact_reviews(db, owner_id=owner_id)
        if reconciled_reviews:
            logger.info("Closed %s legacy review items for existing Contacts.", reconciled_reviews)
        # Upgrade safety: a Global automation created before takeover scopes
        # existed must not process an unbounded historical backlog.
        from .services.automation import parse_plan
        takeover_enabled = flag_svc.is_agent_takeover_enabled(db)
        global_automations = db.query(models.Automation).filter_by(scope="global").all()
        for automation in global_automations:
            plan = parse_plan(automation.plan_json)
            if not plan.takeover_scope:
                automation.status = "disabled"
                automation.next_run_at = None
                takeover_enabled = False
                flag_svc.set_flag(db, "agent_takeover_enabled", "false")
                db.add(models.AuditLog(
                    actor="system", action="global_takeover_scope_required",
                    entity="automation", entity_id=str(automation.id),
                    detail="Disabled during upgrade; select a takeover scope before re-enabling.",
                ))
            desired_mode = "full_auto" if takeover_enabled else "semi_auto"
            desired_status = "enabled" if takeover_enabled and plan.takeover_scope else "disabled"
            changed = automation.execution_mode != desired_mode or automation.status != desired_status
            automation.execution_mode = desired_mode
            automation.status = desired_status
            # TACWork exclusively owns Global cadence; Huey must never enqueue it.
            automation.next_run_at = None
            if plan.execution_mode != desired_mode:
                plan.execution_mode = desired_mode
                automation.plan_json = json.dumps(plan.model_dump())
                changed = True
            if changed:
                db.add(models.AuditLog(
                    actor="system", action="agent_takeover_startup_reconciled",
                    entity="automation", entity_id=str(automation.id),
                    detail=json.dumps({
                        "enabled": takeover_enabled,
                        "execution_mode": desired_mode,
                    }),
                ))

        # The owner-level Approval Mode is independent from TACWork Takeover.
        # Takeover enable/disable may change it through its explicit API, but a
        # service restart must preserve a deliberate Agent Review selection so
        # Campaign first-email generation does not silently revert to pending.
        if not takeover_enabled:
            # A service restart must not leave an inactive switch pointing to
            # a deleted TACWork session from a previous process lifetime.
            from .services import agent_takeover as takeover_svc
            interval = int(flag_svc.get_flag(db, "agent_takeover_interval_minutes", "60") or 60)
            takeover_svc.configure(db, enabled=False, interval_minutes=interval)
        db.commit()
    finally:
        db.close()
    if settings.ENABLE_SCHEDULER:
        start_scheduler(settings.POLL_INTERVAL_SECONDS)
    yield


app = FastAPI(title="Gmail Outreach Agent", lifespan=lifespan)
register_error_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(gmail.router)
app.include_router(campaigns.router)
app.include_router(contacts.router)
app.include_router(inbox.router)
app.include_router(approvals.router)
app.include_router(comparisons.router)
app.include_router(dashboard.router)
app.include_router(system.router)
app.include_router(automation.router)
app.include_router(agent_runs.router)
app.include_router(knowledge.router)
app.include_router(agent_profile.router)
app.include_router(agent_takeover.router)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    """Assign each request a correlation id and log start/end with timing.

    The id is echoed back as ``X-Request-ID`` and stored in a contextvar so all
    log lines emitted while handling the request share it (grep by id to trace a
    single request / run end-to-end). An inbound ``X-Request-ID`` is reused so
    callers can correlate across services.
    """
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    token = trace_id_var.set(rid)
    start = time.perf_counter()
    logger.info("req start %s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("req error %s %s", request.method, request.url.path)
        trace_id_var.reset(token)
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info("req done %s %s -> %s (%.1fms)", request.method, request.url.path, response.status_code, elapsed_ms)
    trace_id_var.reset(token)
    response.headers["X-Request-ID"] = rid
    return response


def _probe_db(db: Session) -> bool:
    """True iff the DB is reachable. Keeps /api/health from being an empty gate."""
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    # The health gate is no longer a rubber stamp: status reflects DB reachability
    # so AGENTS.md's "only proceed when status==ok" is actually enforced. Gmail
    # connection, global pause and LLM config are merged in so an Agent needs a
    # single call to decide whether it may act.
    db_ok = _probe_db(db)
    gmail_account, _oauth = resolve_sending_account(
        db, owner_id=ensure_owner(db), provision=False
    )
    gmail_connected = bool(
        gmail_account and gmail_account.is_connected and gmail_account.oauth
    )
    _llm_config, llm_state = email_config_state(db)
    return {
        "status": "ok" if db_ok else "error",
        "db_ok": db_ok,
        "instance": {
            "install_root": str(Path(__file__).resolve().parents[2]),
            "data_root": str(_DATA.root.resolve()),
        },
        "real_send": is_real_send_enabled(settings, gmail_account, _oauth),
        "gmail_configured": is_gmail_configured(settings),
        "gmail_connected": gmail_connected,
        "gmail_account": gmail_account.email if gmail_account else None,
        "global_pause": flag_svc.is_globally_paused(db),
        "llm_configured": bool(llm_state["usable"]),
        "llm_config": llm_state,
        "consumer": read_consumer_status(),
    }


@app.get("/api/events")
async def events():
    async def event_stream():
        try:
            while True:
                try:
                    evt = await asyncio.wait_for(event_queue().get(), timeout=15)
                    yield f"data: {__import__('json').dumps(evt, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        except asyncio.CancelledError:
            return
    return StreamingResponse(event_stream(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
