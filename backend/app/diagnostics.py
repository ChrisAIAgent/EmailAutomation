"""Unified diagnostic chain.

A single call to :func:`run_diagnostics` probes every subsystem that can fail
silently and returns a structured, real-data snapshot:

    runtime · database · queue(consumer) · gmail(oauth) · ai · scheduler ·
    automations · tacwork · sending · disk

Each probe yields a :class:`DiagnosticResult` with ``status`` (ok / warn /
error / info), a human ``detail``, and an optional ``remedy``. The aggregate
``overall`` verdict lets the UI / Electron / PowerShell show one Red/Yellow/Green
signal and point the operator at the exact broken link.

Design rules (consistent with project conventions):
- Real data only. No fabricated statuses, no "assume ok".
- Best-effort per probe: one probe failing must not abort the others.
- Never returns secrets, tokens, keys, recipients, or bodies.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import inspect, text

from .config import _DATA, get_diagnostic_settings, is_gmail_configured, is_llm_configured
from .consumer_status import read_consumer_status
from .logging_config import get_trace_id
from .models import Automation, GmailAccount, OAuthCredential
from .redact import mask_email
from .services import flags as flag_svc
from .services.ai_config import peek_email_config
from .services.real_send import is_real_send_enabled

# Thresholds (tunable).
_TOKEN_EXPIRY_WARN_SECONDS = 10 * 60          # OAuth token about to expire
_SCHEDULER_STALE_MULTIPLIER = 3               # heartbeat older than interval*3 => stale
_AUTOMATION_STALE_SECONDS = 5 * 60            # next_run_at older than this => suspicious
_DISK_WARN_BYTES = 2 * 1024**3                # < 2 GiB free => warn
_DISK_ERROR_BYTES = 500 * 1024**2             # < 500 MiB free => error
_TACWORK_TIMEOUT_SECONDS = 8
_LOG = logging.getLogger(__name__)


@dataclass
class DiagnosticResult:
    id: str
    category: str
    label: str
    status: str                       # ok | warn | error | info
    detail: str = ""
    remedy: Optional[str] = None
    latency_ms: Optional[float] = None
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _as_utc(value: datetime) -> datetime:
    """Normalize persisted and file-backed timestamps for read-only checks.

    SQLite does not preserve timezone information for ``DateTime`` columns.
    Email Automation writes its persisted diagnostic timestamps in UTC, so a
    naive value read back from SQLite (or a legacy heartbeat file) is UTC too.
    Keep the normalization local to diagnostics: no data migration or write is
    needed merely to inspect these values.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _append_probe_failure(
    items: list[DiagnosticResult],
    *,
    id: str,
    category: str,
    label: str,
    exc: Exception,
    remedy: str = "Retry the manual check; use the trace id to inspect backend logs.",
) -> None:
    """Record one failed probe without exposing exception text or aborting peers."""
    trace_id = get_trace_id()
    _LOG.warning(
        "diagnostics probe failed probe=%s trace_id=%s exception=%s",
        id,
        trace_id,
        type(exc).__name__,
    )
    items.append(DiagnosticResult(
        id=id,
        category=category,
        label=label,
        status="error",
        detail=f"probe failed: {type(exc).__name__} (trace={trace_id})",
        remedy=remedy,
    ))


def _dir_state(path: str) -> dict:
    """Read-only directory state for one data subdir.

    Diagnostics must never create, write or delete anything under the data
    directory, so writability is *inferred* from OS access flags instead of a
    write probe (the old ``.ea_write_test`` file is gone). On Windows the flag
    is a heuristic — it cannot see ACL Deny entries — which is why the detail
    text always says "inferred".
    """
    p = Path(path)
    try:
        exists = p.is_dir()
        return {
            "exists": exists,
            "readable": bool(exists and os.access(p, os.R_OK)),
            "writable_hint": bool(exists and os.access(p, os.W_OK)),
        }
    except Exception:
        return {"exists": False, "readable": False, "writable_hint": False}


def _tacwork_health(settings) -> tuple[bool, str]:
    """Return (reachable, detail). Probes the loopback TACWork server /health."""
    url = f"{settings.TACWORK_SERVER_URL.rstrip('/')}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_TACWORK_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8", "ignore") or "{}"
        data = json.loads(body) if body.strip() else {}
        ok = bool(data.get("ok"))
        return ok, "ok" if ok else f"unexpected body: {body[:120]}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # URLError / timeout
        return False, f"unreachable: {type(e).__name__}"


def _run_diagnostics_inner(db) -> dict:
    s = get_diagnostic_settings()
    now = datetime.now(timezone.utc)
    items: list[DiagnosticResult] = []

    # ---- 1. Runtime / data directory ------------------------------------
    info = {
        "mode": _DATA.mode,
        "root": str(_DATA.root),
        "config": str(_DATA.config),
        "database": str(_DATA.database),
        "queue": str(_DATA.queue),
        "logs": str(_DATA.logs),
    }
    env_file = _DATA.config / ".env"
    states = {k: _dir_state(str(getattr(_DATA, k))) for k in ("config", "database", "queue", "logs")}
    missing = [k for k, st in states.items() if not st["exists"]]
    unreadable = [k for k, st in states.items() if st["exists"] and not st["readable"]]
    if missing or unreadable:
        detail_bits = []
        if missing:
            detail_bits.append(f"missing: {', '.join(missing)}")
        if unreadable:
            detail_bits.append(f"unreadable: {', '.join(unreadable)}")
        items.append(DiagnosticResult(
            id="runtime.writable", category="runtime",
            label="Runtime data directory", status="error",
            detail=f"data dir mode={_DATA.mode}; " + "; ".join(detail_bits),
            remedy="Run as the same Windows user that installed the app, or repair the LOCALAPPDATA\\TAC AISolution permission.",
        ))
    else:
        items.append(DiagnosticResult(
            id="runtime.writable", category="runtime",
            label="Runtime data directory", status="ok",
            detail=(
                f"mode={_DATA.mode}; env_file={'present' if env_file.exists() else 'missing'}; "
                "all subdirs exist and are readable (writability inferred, no write probe)"
            ),
        ))

    # ---- 2. Database -----------------------------------------------------
    db_ok = False
    tables_ok = False
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
        tables_ok = inspect(db.get_bind()).has_table("gmail_accounts")
    except Exception as e:
        items.append(DiagnosticResult(
            id="database.reachable", category="database",
            label="Database", status="error",
            detail=f"unreachable: {type(e).__name__}",
            remedy="Check the SQLite file under the data directory; run a migration if the schema is missing.",
        ))
    if db_ok:
        items.append(DiagnosticResult(
            id="database.reachable", category="database",
            label="Database", status="ok" if tables_ok else "warn",
            detail="reachable" + ("" if tables_ok else "; core table gmail_accounts missing"),
            remedy=None if tables_ok else "Run backend migrations (additive ALTERs) to create missing tables.",
        ))

    # ---- 3. Queue / Huey consumer --------------------------------------
    try:
        cs = read_consumer_status()
        if cs.get("healthy"):
            age = cs.get("age_seconds") or 0
            status = "ok" if age <= 60 else "warn"
            items.append(DiagnosticResult(
                id="queue.consumer", category="queue",
                label="Huey consumer", status=status,
                detail=f"state={cs.get('state')}; heartbeat age={age}s; pid={cs.get('pid')}",
                remedy=None if status == "ok" else "Consumer heartbeat is stale; restart the unified stack.",
            ))
        else:
            items.append(DiagnosticResult(
                id="queue.consumer", category="queue",
                label="Huey consumer", status="error",
                detail=f"state={cs.get('state')}; healthy={cs.get('healthy')}",
                remedy="Start the Huey consumer (start-stack.ps1 / Email Automation.exe). Background jobs will not run until it is up.",
            ))
    except Exception as exc:
        _append_probe_failure(
            items, id="queue.consumer", category="queue", label="Huey consumer", exc=exc,
        )

    # ---- 4. Gmail OAuth -------------------------------------------------
    account = None
    oauth_expiry: Optional[datetime] = None
    try:
        gmail_cfg = is_gmail_configured(s)
        if db_ok:
            account = (
                db.query(GmailAccount)
                .filter(GmailAccount.is_connected == True, GmailAccount.oauth != None)  # noqa: E711
                .first()
            )
            if account and account.oauth and account.oauth.token_expiry:
                oauth_expiry = account.oauth.token_expiry
        if not gmail_cfg:
            items.append(DiagnosticResult(
                id="gmail.oauth", category="gmail",
                label="Gmail OAuth", status="info",
                detail="Google OAuth not configured (no client id/secret).",
            ))
        elif not account:
            items.append(DiagnosticResult(
                id="gmail.oauth", category="gmail",
                label="Gmail OAuth", status="error",
                detail="OAuth credentials are configured but no Gmail account is connected.",
                remedy="Connect a Gmail account in Settings (authorize via Google).",
            ))
        else:
            has_refresh = bool(account.oauth and account.oauth.refresh_token_enc)
            if oauth_expiry:
                secs = (_as_utc(oauth_expiry) - now).total_seconds()
                if secs <= 0 and has_refresh:
                    # Access-token expiry is expected in a refresh-token OAuth
                    # flow.  This read-only check must not misreport a connected
                    # account as failed merely because it cannot refresh here.
                    items.append(DiagnosticResult(
                        id="gmail.oauth", category="gmail",
                        label="Gmail OAuth", status="info",
                        detail=(f"connected as {mask_email(account.email)}; cached access token is expired "
                                "and will refresh automatically before the next Gmail call"),
                    ))
                elif secs <= 0:
                    items.append(DiagnosticResult(
                        id="gmail.oauth", category="gmail",
                        label="Gmail OAuth", status="warn",
                        detail=f"access token expired {abs(int(secs))}s ago and no refresh token is available",
                        remedy="Re-authorize the Gmail account.",
                    ))
                elif secs <= _TOKEN_EXPIRY_WARN_SECONDS:
                    items.append(DiagnosticResult(
                        id="gmail.oauth", category="gmail",
                        label="Gmail OAuth", status="info",
                        detail=(f"connected as {mask_email(account.email)}; access token expires in "
                                f"{int(secs)}s and will auto-refresh"),
                    ))
                else:
                    items.append(DiagnosticResult(
                        id="gmail.oauth", category="gmail",
                        label="Gmail OAuth", status="ok",
                        detail=f"connected as {mask_email(account.email)}",
                    ))
            else:
                items.append(DiagnosticResult(
                    id="gmail.oauth", category="gmail",
                    label="Gmail OAuth", status="ok",
                    detail=f"connected as {mask_email(account.email)}",
                ))
    except Exception as exc:
        account = None
        _append_probe_failure(
            items, id="gmail.oauth", category="gmail", label="Gmail OAuth", exc=exc,
        )

    # ---- 5. AI / LLM configuration -------------------------------------
    try:
        ai_cfg = bool(peek_email_config(db)) or is_llm_configured(s)
        if ai_cfg:
            items.append(DiagnosticResult(
                id="ai.config", category="ai",
                label="AI / LLM", status="ok",
                detail=f"provider configured (model={s.effective_llm_model or 'n/a'})",
            ))
        else:
            items.append(DiagnosticResult(
                id="ai.config", category="ai",
                label="AI / LLM", status="error",
                detail="No AI provider key/model configured.",
                remedy="Set the Email Agent LLM Base URL, model and API key in Agent Settings.",
            ))
    except Exception as exc:
        _append_probe_failure(
            items, id="ai.config", category="ai", label="AI / LLM", exc=exc,
        )

    # ---- 6. Scheduler ---------------------------------------------------
    enabled = bool(s.ENABLE_SCHEDULER)
    interval = int(s.POLL_INTERVAL_SECONDS)
    hb_path = _DATA.queue / "scheduler-heartbeat.json"
    hb_age: Optional[float] = None
    if hb_path.exists():
        try:
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
            heartbeat_at = _as_utc(datetime.fromisoformat(hb["tick_at"]))
            hb_age = max(0.0, (now - heartbeat_at).total_seconds())
        except Exception:
            hb_age = None
    if not enabled:
        items.append(DiagnosticResult(
            id="scheduler.loop", category="scheduler",
            label="Scheduler", status="info",
            detail="disabled (ENABLE_SCHEDULER=false).",
        ))
    elif hb_age is None:
        items.append(DiagnosticResult(
            id="scheduler.loop", category="scheduler",
            label="Scheduler", status="warn",
            detail=f"enabled (interval={interval}s) but no heartbeat marker yet",
            remedy="Wait for the next tick, or restart the backend if the scheduler thread did not start.",
        ))
    elif hb_age > interval * _SCHEDULER_STALE_MULTIPLIER:
        items.append(DiagnosticResult(
            id="scheduler.loop", category="scheduler",
            label="Scheduler", status="error",
            detail=f"enabled but heartbeat is stale ({int(hb_age)}s, interval={interval}s)",
            remedy="The scheduler thread likely crashed; restart the backend/consumer.",
        ))
    else:
        items.append(DiagnosticResult(
            id="scheduler.loop", category="scheduler",
            label="Scheduler", status="ok",
            detail=f"ticking (last heartbeat {int(hb_age)}s ago, interval={interval}s)",
        ))

    # ---- 7. Automations (stuck detection) ------------------------------
    if db_ok:
        try:
            autos = db.query(Automation).filter(Automation.status == "enabled").all()
            enabled_count = len(autos)
            stale = [
                a.id for a in autos
                if a.next_run_at and (_as_utc(a.next_run_at) - now).total_seconds() < -_AUTOMATION_STALE_SECONDS
            ]
            if stale:
                items.append(DiagnosticResult(
                    id="automations.stuck", category="automations",
                    label="Automations", status="warn",
                    detail=f"{enabled_count} enabled; {len(stale)} have a past-due schedule (ids: {stale[:5]}) — scheduler may be stuck",
                    remedy="Verify the scheduler heartbeat; a dead scheduler leaves enabled automations unscheduled.",
                ))
            else:
                items.append(DiagnosticResult(
                    id="automations.stuck", category="automations",
                    label="Automations", status="ok",
                    detail=f"{enabled_count} enabled, none past-due",
                ))
        except Exception as exc:
            _append_probe_failure(
                items, id="automations.stuck", category="automations", label="Automations", exc=exc,
            )

    # ---- 8. Agent Takeover / TACWork -----------------------------------
    try:
        takeover_enabled = flag_svc.is_agent_takeover_enabled(db) if db_ok else False
        if not takeover_enabled:
            items.append(DiagnosticResult(
                id="tacwork.connection", category="tacwork",
                label="Agent Takeover / TACWork", status="info",
                detail="Agent takeover is disabled.",
            ))
        else:
            ok, detail = _tacwork_health(s)
            items.append(DiagnosticResult(
                id="tacwork.connection", category="tacwork",
                label="Agent Takeover / TACWork", status="ok" if ok else "error",
                detail=f"enabled; server {s.TACWORK_SERVER_URL} -> {detail}",
                remedy=None if ok else "Start the embedded TACWork runtime (Email Automation.exe manages it).",
            ))
    except Exception as exc:
        _append_probe_failure(
            items, id="tacwork.connection", category="tacwork", label="Agent Takeover / TACWork", exc=exc,
        )

    # ---- 9. Knowledge base ---------------------------------------------
    try:
        from .knowledge import retrieve_knowledge
        kb = retrieve_knowledge("__diagnostic_probe__", limit=1)
        kb_ok = isinstance(kb, (list, tuple))
        items.append(DiagnosticResult(
            id="knowledge.base", category="knowledge",
            label="Knowledge base", status="ok" if kb_ok else "info",
            detail="retriever available" if kb_ok else "retriever returned empty",
        ))
    except Exception as e:
        items.append(DiagnosticResult(
            id="knowledge.base", category="knowledge",
            label="Knowledge base", status="warn",
            detail=f"retriever error: {type(e).__name__}: {str(e)[:80]}",
            remedy="Check the knowledge module import / data; reply strategy falls back to defaults.",
        ))

    # ---- 10. Sending safety --------------------------------------------
    # An empty RESTRICTED_RECIPIENT_ALLOWLIST is a *supported* configuration:
    # the policy engine only enforces the allowlist when it is configured.
    try:
        real_send = is_real_send_enabled(s, account, account.oauth if account else None) if account else False
        allowlist = s.recipient_allowlist
        if not real_send:
            items.append(DiagnosticResult(
                id="sending.safety", category="sending",
                label="Sending", status="info",
                detail="real send disabled (safe default).",
            ))
        elif not allowlist:
            items.append(DiagnosticResult(
                id="sending.safety", category="sending",
                label="Sending", status="info",
                detail="real send enabled; RESTRICTED_RECIPIENT_ALLOWLIST is empty (no allowlist configured).",
            ))
        else:
            items.append(DiagnosticResult(
                id="sending.safety", category="sending",
                label="Sending", status="ok",
                detail=f"real send enabled; RESTRICTED_RECIPIENT_ALLOWLIST has {len(allowlist)} address(es).",
            ))
    except Exception as exc:
        _append_probe_failure(
            items, id="sending.safety", category="sending", label="Sending", exc=exc,
        )

    # ---- 11. Disk space ------------------------------------------------
    try:
        du = shutil.disk_usage(str(_DATA.root))
        free = du.free
        if free < _DISK_ERROR_BYTES:
            items.append(DiagnosticResult(
                id="disk.space", category="disk",
                label="Disk space", status="error",
                detail=f"only {free // 1024**2} MiB free on data volume",
                remedy="Free disk space; the database and queue logs will stop writing otherwise.",
            ))
        elif free < _DISK_WARN_BYTES:
            items.append(DiagnosticResult(
                id="disk.space", category="disk",
                label="Disk space", status="warn",
                detail=f"{free // 1024**3} GiB free on data volume",
                remedy="Disk is getting low; plan to free space soon.",
            ))
        else:
            items.append(DiagnosticResult(
                id="disk.space", category="disk",
                label="Disk space", status="ok",
                detail=f"{free // 1024**3} GiB free",
            ))
    except Exception:
        pass

    # ---- Aggregate ------------------------------------------------------
    counts = {"ok": 0, "warn": 0, "error": 0, "info": 0}
    for it in items:
        counts[it.status] = counts.get(it.status, 0) + 1
    if counts["error"]:
        overall = "error"
    elif counts["warn"]:
        overall = "degraded"
    else:
        overall = "ok"

    by_category: dict[str, list[dict]] = {}
    for it in items:
        by_category.setdefault(it.category, []).append(asdict(it))

    return {
        "generated_at": now.isoformat(),
        "trace_id": get_trace_id(),
        "overall": overall,
        "counts": counts,
        "items": [asdict(i) for i in items],
        "by_category": by_category,
    }


def run_diagnostics(db) -> dict:
    """Overview snapshot. If the overview itself fails, it must report
    ``overall="unknown"`` — never a green light."""
    try:
        return _run_diagnostics_inner(db)
    except Exception as exc:
        failure = DiagnosticResult(
            id="diagnostics.overview", category="runtime",
            label="Diagnostics overview", status="error",
            detail=f"overview execution failed: {type(exc).__name__}",
            remedy="Retry the manual check; inspect backend-error.log for the stack trace.",
        )
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": get_trace_id(),
            "overall": "unknown",
            "counts": {"ok": 0, "warn": 0, "error": 1, "info": 0, "unknown": 0},
            "items": [asdict(failure)],
            "by_category": {"runtime": [asdict(failure)]},
            "error": str(exc)[:200],
        }
