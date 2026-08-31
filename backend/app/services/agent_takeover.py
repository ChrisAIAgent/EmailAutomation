"""Scheduled TACWork takeover orchestration.

The Email Automation worker owns cadence and authorization state.  Each due
tick creates a fresh TACWork root session with one fixed operational prompt;
TACWork then uses the existing MCP tools and the normal backend policy engine.
"""
from __future__ import annotations

import json
import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .. import models
from ..config import get_settings
from . import flags
from . import workspace_time

logger = logging.getLogger("agent.takeover")

MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 1440

FLAG_ENABLED = "agent_takeover_enabled"
FLAG_INTERVAL = "agent_takeover_interval_minutes"
FLAG_NEXT_RUN = "agent_takeover_next_run_at"
FLAG_LAST_RUN = "agent_takeover_last_run_at"
FLAG_LAST_STATUS = "agent_takeover_last_status"
FLAG_LAST_ERROR = "agent_takeover_last_error"
FLAG_ACTIVE_SESSION = "agent_takeover_active_session_id"
FLAG_LAST_SESSION = "agent_takeover_last_session_id"
FLAG_WORKSPACE = "agent_takeover_workspace_id"
FLAG_TOKEN_HASH = "agent_takeover_token_hash"
FLAG_TOKEN_EXPIRES = "agent_takeover_token_expires_at"
FLAG_CYCLE_ID = "agent_takeover_cycle_id"
FLAG_CURRENT_STAGE = "agent_takeover_current_stage"
FLAG_LAST_SUCCESS_STAGE = "agent_takeover_last_success_stage"
FLAG_LAST_COMPLETED = "agent_takeover_last_completed_at"


def _parse_time(value: str | None) -> datetime | None:
    return workspace_time.as_utc(value)


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    settings = get_settings()
    base = settings.TACWORK_SERVER_URL.rstrip("/")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = Request(
        f"{base}{path}", data=body, method=method,
        headers={
            "Authorization": f"Bearer {settings.TACWORK_CLIENT_TOKEN}",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urlopen(req, timeout=settings.TACWORK_HTTP_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"tacwork_http_{exc.code}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"tacwork_unreachable: {exc}") from exc


def _operating_prompt(token: str, cycle_id: str, db) -> str:
    timezone_name = workspace_time.get_timezone(db)
    started_at = workspace_time.display(datetime.now(timezone.utc), timezone_name)["local"]
    next_run = workspace_time.display(
        datetime.now(timezone.utc) + timedelta(minutes=int(flags.get_flag(db, FLAG_INTERVAL, "60") or 60)),
        timezone_name,
    )["local"]
    return f"""You are the scheduled production Email Automation operating Agent. This is a fresh, isolated run authorized by the Workspace's active Agent Takeover switch.

Workspace display timezone: {timezone_name}. Use this time zone for every human-readable time in the final Chinese report. This cycle started at: {started_at}. The next Agent Takeover wake-up is: {next_run}. Do not print raw UTC timestamps or report the legacy Global Automation next_run_at as a separate scan; Global Inbox is owned by Agent Takeover and Huey does not execute it.

Execute exactly one Global Inbox operating cycle and do not create Campaign outreach:
1. Call ea_takeover_status and re-read current health, Gmail, pause, approval mode, queues and automation state.
2. If the system is paused, Gmail/consumer/AI is unavailable, the first Gmail history import is incomplete, or another Global Inbox Run is active, do not write or retry; report the exact blocker and finish. Scheduled takeover never starts the first full import.
Cycle ID: {cycle_id}
3. Call ea_sync_gmail, then ea_daily_triage_status. If untriaged conversations exist and no triage Run is active, call ea_start_daily_triage with user_authorized=true, authorization_source=agent_takeover and takeover_token={token}. Its fixed snapshot may exceed 50 conversations; 50 is only the safe worker batch size. If a daily Run is active, read and report its real progress rather than creating a duplicate. Newly synced mail during a Run waits for the next cycle.
4. Start only the enabled Automation work that the current triaged, admitted Contacts actually require. Use ea_start_agent_run with user_authorized=true, authorization_source=agent_takeover and takeover_token={token}; do not create duplicate or irrelevant Campaign work.
5. Follow that same Run with ea_get_agent_run, authorization_source=agent_takeover and takeover_token={token}, until it reaches a terminal status. Never start a duplicate Run and never treat queued/running, a Draft, Approval, or HTTP 200 as sent.
6. Leave contact_admission_uncertain, content_uncertain, opt_out_confirmation and every other human-review item for a person; report and skip them without blocking eligible admitted Contacts.
7. Finish with a concise Chinese operating report containing the supplied local execution time and next Agent Takeover time (including {timezone_name}), Gmail account, sync/triage counts and progress, filtered and human-review counts, Run ID/status, drafts/approvals, actual Gmail-accepted sends/message IDs, stops, skips and failures. Only Gmail acceptance may be reported as sent.

The Agent Takeover switch authorizes routine Inbox operation and eligible admitted-contact follow-up within this cycle. It never authorizes Contact admission, deletion, or bypassing pause, suppression, send windows, daily limits, idempotency, thread integrity, OAuth, delivery reconciliation, or any human-review gate."""


def token_is_valid(db, token: str) -> bool:
    if not flags.is_agent_takeover_enabled(db) or not token:
        return False
    expected = flags.get_flag(db, FLAG_TOKEN_HASH, "") or ""
    expires = _parse_time(flags.get_flag(db, FLAG_TOKEN_EXPIRES))
    actual = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return bool(expected and expires and expires > datetime.now(timezone.utc) and hmac.compare_digest(expected, actual))


def status(db) -> dict:
    interval = int(flags.get_flag(db, FLAG_INTERVAL, "60") or 60)
    display_timezone = workspace_time.get_timezone(db)
    next_run = _parse_time(flags.get_flag(db, FLAG_NEXT_RUN))
    last_run = _parse_time(flags.get_flag(db, FLAG_LAST_RUN))
    completed_at = _parse_time(flags.get_flag(db, FLAG_LAST_COMPLETED))
    return {
        "enabled": flags.is_agent_takeover_enabled(db),
        "interval_minutes": interval,
        "next_run_at": next_run,
        "last_run_at": last_run,
        "last_completed_at": completed_at,
        "display_timezone": display_timezone,
        "display_time": {
            "next_run_at": workspace_time.display(next_run, display_timezone),
            "last_run_at": workspace_time.display(last_run, display_timezone),
            "last_completed_at": workspace_time.display(completed_at, display_timezone),
        },
        "last_status": flags.get_flag(db, FLAG_LAST_STATUS),
        "last_error": flags.get_flag(db, FLAG_LAST_ERROR),
        "active_session_id": flags.get_flag(db, FLAG_ACTIVE_SESSION),
        "last_session_id": flags.get_flag(db, FLAG_LAST_SESSION),
        "workspace_id": flags.get_flag(db, FLAG_WORKSPACE),
        "cycle_id": flags.get_flag(db, FLAG_CYCLE_ID),
        "current_stage": flags.get_flag(db, FLAG_CURRENT_STAGE),
        "last_success_stage": flags.get_flag(db, FLAG_LAST_SUCCESS_STAGE),
    }


def configure(db, *, enabled: bool, interval_minutes: int, display_timezone: str | None = None) -> dict:
    if not isinstance(interval_minutes, int) or isinstance(interval_minutes, bool) or not (
        MIN_INTERVAL_MINUTES <= interval_minutes <= MAX_INTERVAL_MINUTES
    ):
        raise ValueError("unsupported_agent_takeover_interval")
    now = datetime.now(timezone.utc)
    previously_enabled = flags.is_agent_takeover_enabled(db)
    if display_timezone:
        workspace_time.set_timezone(db, display_timezone)
    flags.set_flag(db, FLAG_ENABLED, "true" if enabled else "false")
    flags.set_flag(db, FLAG_INTERVAL, str(interval_minutes))
    flags.set_flag(
        db, FLAG_NEXT_RUN,
        (now + timedelta(minutes=interval_minutes)).isoformat() if enabled else "",
    )
    # A stopped session must never be adopted by a later takeover cycle. This
    # only clears on a switch transition, so an interval-only edit cannot stop
    # a TACWork session that is genuinely still running.
    if not enabled or not previously_enabled:
        flags.set_flag(db, FLAG_ACTIVE_SESSION, "")
        flags.set_flag(db, FLAG_WORKSPACE, "")
        flags.set_flag(db, FLAG_CYCLE_ID, "")
        flags.set_flag(db, FLAG_LAST_STATUS, "disabled")
        flags.set_flag(db, FLAG_LAST_ERROR, "")
        flags.set_flag(db, FLAG_TOKEN_HASH, "")
        flags.set_flag(db, FLAG_TOKEN_EXPIRES, "")
        flags.set_flag(db, FLAG_CURRENT_STAGE, "disabled")
    return status(db)


def _session_is_busy(workspace_id: str, session_id: str) -> bool:
    snapshot = _request(
        "GET",
        f"/workspace/{workspace_id}/sessions/{session_id}/snapshot?limit=20",
    )
    return (snapshot.get("status") or {}).get("type") not in {None, "idle"}


def trigger_due(db, *, force: bool = False) -> dict:
    current = status(db)
    if not current["enabled"]:
        return {"status": "disabled"}
    now = datetime.now(timezone.utc)
    interval = current["interval_minutes"]

    active_session = current["active_session_id"]
    workspace_id = current["workspace_id"]
    if active_session and workspace_id:
        try:
            if _session_is_busy(workspace_id, active_session):
                if not force and current["next_run_at"] and current["next_run_at"] > now:
                    return {"status": "running", "session_id": active_session}
                flags.set_flag(db, FLAG_LAST_STATUS, "previous_run_still_running")
                flags.set_flag(db, FLAG_NEXT_RUN, (now + timedelta(minutes=interval)).isoformat())
                db.commit()
                return {"status": "previous_run_still_running", "session_id": active_session}
            flags.set_flag(db, FLAG_ACTIVE_SESSION, "")
            flags.set_flag(db, FLAG_LAST_STATUS, "completed")
            flags.set_flag(db, FLAG_CURRENT_STAGE, "completed")
            flags.set_flag(db, FLAG_LAST_COMPLETED, now.isoformat())
            flags.set_flag(db, FLAG_TOKEN_HASH, "")
            flags.set_flag(db, FLAG_TOKEN_EXPIRES, "")
            db.add(models.AuditLog(
                actor="scheduler", action="agent_takeover_session_completed",
                entity="tacwork_session", entity_id=active_session,
                detail=json.dumps({"cycle_id": current["cycle_id"], "completed_at_utc": now.isoformat(),
                                   "display_timezone": current["display_timezone"],
                                   "completed_at_display": workspace_time.display(now, current["display_timezone"])["local"]}),
            ))
            db.commit()
        except Exception as exc:
            logger.warning("could not read prior TACWork session %s: %s", active_session, exc)
            # A deleted TACWork session is a stale local reference, not an
            # unknown email-delivery state. Clear it and continue this cycle.
            if "tacwork_http_404" in str(exc) and "session_not_found" in str(exc):
                flags.set_flag(db, FLAG_ACTIVE_SESSION, "")
                flags.set_flag(db, FLAG_WORKSPACE, "")
                flags.set_flag(db, FLAG_LAST_STATUS, "stale_session_recovered")
                flags.set_flag(db, FLAG_LAST_ERROR, "")
                flags.set_flag(db, FLAG_CURRENT_STAGE, "stale_session_recovered")
                db.add(models.AuditLog(
                    actor="scheduler", action="agent_takeover_stale_session_recovered",
                    entity="tacwork_session", entity_id=active_session,
                    detail=json.dumps({"reason": "session_not_found"}),
                ))
                db.commit()
            else:
                flags.set_flag(db, FLAG_LAST_STATUS, "previous_run_status_unknown")
                flags.set_flag(db, FLAG_LAST_ERROR, str(exc)[:500])
                flags.set_flag(db, FLAG_NEXT_RUN, (now + timedelta(minutes=interval)).isoformat())
                db.commit()
                return {"status": "previous_run_status_unknown", "session_id": active_session}

    if not force and current["next_run_at"] and current["next_run_at"] > now:
        return {"status": "not_due", "next_run_at": current["next_run_at"]}

    if flags.is_globally_paused(db):
        flags.set_flag(db, FLAG_LAST_STATUS, "blocked_paused")
        flags.set_flag(db, FLAG_LAST_ERROR, "global_pause")
        flags.set_flag(db, FLAG_NEXT_RUN, (now + timedelta(minutes=interval)).isoformat())
        db.commit()
        return {"status": "blocked_paused"}

    try:
        cycle_id = uuid.uuid4().hex
        flags.set_flag(db, FLAG_CYCLE_ID, cycle_id)
        flags.set_flag(db, FLAG_CURRENT_STAGE, "tacwork_status")
        db.commit()
        server_status = _request("GET", "/status")
        workspace_id = str(server_status.get("activeWorkspaceId") or "").strip()
        if not workspace_id:
            raise RuntimeError("tacwork_has_no_active_workspace")
        display_timezone = workspace_time.get_timezone(db)
        title = f"Inbox Operation · {workspace_time.display(now, display_timezone)['local']}"
        takeover_token = secrets.token_urlsafe(32)
        # Publish the short-lived capability before prompt_async starts so the
        # first fast MCP call cannot race authorization persistence.
        flags.set_flag(db, FLAG_TOKEN_HASH, hashlib.sha256(takeover_token.encode("utf-8")).hexdigest())
        flags.set_flag(db, FLAG_TOKEN_EXPIRES, (now + timedelta(minutes=max(interval, 120))).isoformat())
        flags.set_flag(db, FLAG_LAST_SUCCESS_STAGE, "tacwork_status")
        flags.set_flag(db, FLAG_CURRENT_STAGE, "session_create")
        db.commit()
        created = _request(
            "POST", f"/workspace/{workspace_id}/sessions",
            {"title": title, "prompt": _operating_prompt(takeover_token, cycle_id, db)},
        )
        session_id = str((created.get("item") or {}).get("id") or "").strip()
        if not session_id:
            raise RuntimeError("tacwork_session_id_missing")
        flags.set_flag(db, FLAG_WORKSPACE, workspace_id)
        flags.set_flag(db, FLAG_ACTIVE_SESSION, session_id)
        flags.set_flag(db, FLAG_LAST_SESSION, session_id)
        flags.set_flag(db, FLAG_LAST_RUN, now.isoformat())
        flags.set_flag(db, FLAG_LAST_STATUS, "running")
        flags.set_flag(db, FLAG_LAST_ERROR, "")
        flags.set_flag(db, FLAG_CURRENT_STAGE, "agent_running")
        flags.set_flag(db, FLAG_LAST_SUCCESS_STAGE, "session_create")
        flags.set_flag(db, FLAG_NEXT_RUN, (now + timedelta(minutes=interval)).isoformat())
        db.add(models.AuditLog(
            actor="scheduler", action="agent_takeover_session_started",
            entity="tacwork_session", entity_id=session_id,
            detail=json.dumps({"cycle_id": cycle_id, "workspace_id": workspace_id, "interval_minutes": interval,
                               "started_at_utc": now.isoformat(), "display_timezone": display_timezone,
                               "started_at_display": workspace_time.display(now, display_timezone)["local"]}),
        ))
        db.commit()
        return {"status": "running", "session_id": session_id, "workspace_id": workspace_id}
    except Exception as exc:
        failed_stage = flags.get_flag(db, FLAG_CURRENT_STAGE)
        flags.set_flag(db, FLAG_LAST_STATUS, "failed")
        flags.set_flag(db, FLAG_LAST_ERROR, str(exc)[:500])
        flags.set_flag(db, FLAG_LAST_RUN, now.isoformat())
        flags.set_flag(db, FLAG_CURRENT_STAGE, "failed")
        flags.set_flag(db, FLAG_TOKEN_HASH, "")
        flags.set_flag(db, FLAG_TOKEN_EXPIRES, "")
        flags.set_flag(db, FLAG_NEXT_RUN, (now + timedelta(minutes=interval)).isoformat())
        db.add(models.AuditLog(
            actor="scheduler", action="agent_takeover_session_failed",
            entity="agent_takeover", entity_id=flags.get_flag(db, FLAG_CYCLE_ID),
            detail=json.dumps({"stage": failed_stage, "error": str(exc)[:500]}), success=False,
        ))
        db.commit()
        return {"status": "failed", "error": str(exc)[:500]}
