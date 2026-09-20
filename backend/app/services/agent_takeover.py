"""Scheduled TACWork takeover orchestration.

The Email Automation worker owns cadence and authorization state.  Each due
tick creates a fresh TACWork root session with one fixed operational prompt;
TACWork then uses the existing MCP tools and the normal backend policy engine.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from .. import models
from . import flags
from . import workspace_time
from . import agent_providers

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
FLAG_CYCLE_HAS_ERRORS = "agent_takeover_cycle_has_errors"
FLAG_SCOPE = "agent_takeover_scope"
FLAG_SESSION_PROVIDER = agent_providers.SESSION_PROVIDER_FLAG
TAKEOVER_SCOPES = {"inbox", "campaign", "all"}


def _parse_time(value: str | None) -> datetime | None:
    return workspace_time.as_utc(value)


def _operating_prompt(cycle_id: str, db) -> str:
    timezone_name = workspace_time.get_timezone(db)
    started_at = workspace_time.display(datetime.now(timezone.utc), timezone_name)["local"]
    next_run = workspace_time.display(
        datetime.now(timezone.utc) + timedelta(minutes=int(flags.get_flag(db, FLAG_INTERVAL, "60") or 60)),
        timezone_name,
    )["local"]
    scope = scope_value(db)
    campaign_guidance = (
        "8. This takeover explicitly includes Campaign scope. Read current Campaigns and Contacts before writing. You may use ea_find_contacts, ea_update_contact, ea_transition_contact and the Campaign tools for owned records only. Do not import a file, create a Campaign, or change a manually locked Contact unless the user explicitly authorized that action in this cycle. A sales-related reply may qualify a Prospect only when its source Campaign is unambiguous."
        if scope in {"campaign", "all"} else
        "8. This takeover is Inbox-only. Do not create or modify Campaigns, Campaign members, or Contact lifecycle classifications; leave Campaign work for an explicitly enabled campaign/all takeover or a direct user operation."
    )
    return f"""You are the scheduled production Email Automation operating Agent. This is a fresh, isolated run authorized by the Workspace's active Agent Takeover switch.

Workspace display timezone: {timezone_name}. Use this time zone for every human-readable time in the final Chinese report. This cycle started at: {started_at}. The next Agent Takeover wake-up is: {next_run}. Do not print raw UTC timestamps or report the legacy Global Automation next_run_at as a separate scan; Global Inbox is owned by Agent Takeover and Huey does not execute it.

Execute exactly one Global Inbox operating cycle and do not create Campaign outreach:
1. Use only registered typed ea_* MCP tools. Never fall back to REST, shell commands, source-code exploration, or guessed endpoints. A tool error is not evidence that the tool is absent. Do not retry automatically; report the exact tool and structured error, skip that stage, and do not claim it completed.
2. Call ea_takeover_status and use it as the authority for the active takeover grant, health, Gmail, pause, approval mode, and permissions. Use ea_dashboard only for queue metrics. If they conflict, re-read ea_takeover_status once; if the conflict remains, do not write and report state_conflict.
3. If the system is paused, Gmail/consumer/AI is unavailable, the first Gmail history import is incomplete, or another Global Inbox Run is active, do not write or retry; report the exact blocker and finish. Scheduled takeover never starts the first full import.
Cycle ID: {cycle_id}
4. Call ea_sync_gmail, then ea_daily_triage_status. If ea_dashboard reports unprocessed conversations and no triage Run is active, call ea_start_daily_triage. Its fixed snapshot may exceed 50 conversations; 50 is only the safe worker batch size. If a daily Run is active, read and report its real progress rather than creating a duplicate. Newly synced mail during a Run waits for the next cycle.
5. Start only enabled Automation work that the current triaged, admitted Contacts actually require. Then follow that same Run with ea_get_agent_run until it reaches a terminal status. Never start a duplicate Run and never treat queued/running, a Draft, Approval, or HTTP 200 as sent.
6. Leave contact_admission_uncertain, content_uncertain, opt_out_confirmation and every other human-review item for a person; report and skip them without blocking eligible admitted Contacts.
7. Before the final report, call ea_takeover_status again. Use its freshly returned local server time and next-run value. If next_run_at is null or already past, say scheduler reconciliation is pending rather than presenting it as a future wake-up. Finish with a concise Chinese report containing the cycle start and finish time, Gmail account, sync/triage counts and progress, filtered and human-review counts, Run ID/status, drafts/approvals, actual Gmail-accepted sends/message IDs, stops, skips and failures. Only Gmail acceptance may be reported as sent.
{campaign_guidance}

The Agent Takeover switch authorizes only the configured {scope} scope within this cycle. It never authorizes Contact admission, deletion, or bypassing pause, suppression, send windows, daily limits, idempotency, thread integrity, OAuth, delivery reconciliation, or any human-review gate."""


def _operating_system_context(token: str) -> str:
    """Keep the short-lived capability out of the visible session transcript."""
    return f"""This scheduled Agent Takeover session has a sealed local capability.

For every scheduled tool that accepts it, include authorization_source=agent_takeover
and takeover_token={token}. Write tools also require user_authorized=true. Never
reveal, quote, summarize, persist, or place this capability in the final report,
a user-visible message, a file, a command, or another tool argument except the
allowed Email Automation typed MCP tools."""


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
        "cycle_has_errors": (flags.get_flag(db, FLAG_CYCLE_HAS_ERRORS, "false") or "false").lower() == "true",
        "scope": scope_value(db),
        "selected_provider": agent_providers.selected_provider_id(db),
        "session_provider": flags.get_flag(db, FLAG_SESSION_PROVIDER, "") or "",
    }


def scope_value(db) -> str:
    value = (flags.get_flag(db, FLAG_SCOPE, "inbox") or "inbox").strip().lower()
    return value if value in TAKEOVER_SCOPES else "inbox"


def configure(db, *, enabled: bool, interval_minutes: int, display_timezone: str | None = None,
              scope: str | None = None) -> dict:
    if not isinstance(interval_minutes, int) or isinstance(interval_minutes, bool) or not (
        MIN_INTERVAL_MINUTES <= interval_minutes <= MAX_INTERVAL_MINUTES
    ):
        raise ValueError("unsupported_agent_takeover_interval")
    if enabled:
        provider = agent_providers.get_selected_provider(db)
        agent_providers.require_capability(provider, agent_providers.CAP_SCHEDULED_TAKEOVER)
    now = datetime.now(timezone.utc)
    previously_enabled = flags.is_agent_takeover_enabled(db)
    if display_timezone:
        workspace_time.set_timezone(db, display_timezone)
    if scope is not None:
        normalized_scope = scope.strip().lower()
        if normalized_scope not in TAKEOVER_SCOPES:
            raise ValueError("unsupported_agent_takeover_scope")
        flags.set_flag(db, FLAG_SCOPE, normalized_scope)
    elif not flags.get_flag(db, FLAG_SCOPE):
        flags.set_flag(db, FLAG_SCOPE, "inbox")
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
        flags.set_flag(db, FLAG_CYCLE_HAS_ERRORS, "false")
        flags.set_flag(db, FLAG_SESSION_PROVIDER, "")
    return status(db)


def _session_provider(db, current: dict):
    provider_id = current.get("session_provider") or current.get("selected_provider")
    return agent_providers.get_provider(provider_id)


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
            provider = _session_provider(db, current)
            session = provider.get_session(workspace_id=workspace_id, session_id=active_session)
            session_status = session.get("status", "unknown")
            if session_status in {"created", "running", "unknown"}:
                if not force and current["next_run_at"] and current["next_run_at"] > now:
                    return {"status": "running", "session_id": active_session, "provider_id": provider.id}
                flags.set_flag(db, FLAG_LAST_STATUS, "previous_run_still_running")
                flags.set_flag(db, FLAG_NEXT_RUN, (now + timedelta(minutes=interval)).isoformat())
                db.commit()
                return {"status": "previous_run_still_running", "session_id": active_session, "provider_id": provider.id}
            flags.set_flag(db, FLAG_ACTIVE_SESSION, "")
            cycle_has_errors = (flags.get_flag(db, FLAG_CYCLE_HAS_ERRORS, "false") or "false").lower() == "true"
            final_status = "completed_with_errors" if cycle_has_errors else session_status
            if final_status not in {"completed", "completed_with_errors", "failed", "stopped"}:
                final_status = "completed_with_errors" if cycle_has_errors else "completed"
            flags.set_flag(db, FLAG_LAST_STATUS, final_status)
            flags.set_flag(db, FLAG_CURRENT_STAGE, final_status)
            flags.set_flag(db, FLAG_LAST_COMPLETED, now.isoformat())
            flags.set_flag(db, FLAG_TOKEN_HASH, "")
            flags.set_flag(db, FLAG_TOKEN_EXPIRES, "")
            flags.set_flag(db, FLAG_SESSION_PROVIDER, "")
            db.add(models.AuditLog(
                actor="scheduler", action="agent_takeover_session_completed",
                entity="agent_session", entity_id=active_session,
                detail=json.dumps({"cycle_id": current["cycle_id"], "completed_at_utc": now.isoformat(),
                                   "display_timezone": current["display_timezone"],
                                   "completed_at_display": workspace_time.display(now, current["display_timezone"])["local"],
                                   "outcome": final_status, "provider_id": provider.id}),
                success=final_status in {"completed", "stopped"} and not cycle_has_errors,
            ))
            db.commit()
        except Exception as exc:
            logger.warning("could not read prior Agent session %s: %s", active_session, exc)
            # A deleted Agent session is a stale local reference, not an
            # unknown email-delivery state. Clear it and continue this cycle.
            if "session_not_found" in str(exc) or "http_404" in str(exc):
                flags.set_flag(db, FLAG_ACTIVE_SESSION, "")
                flags.set_flag(db, FLAG_WORKSPACE, "")
                flags.set_flag(db, FLAG_SESSION_PROVIDER, "")
                flags.set_flag(db, FLAG_LAST_STATUS, "stale_session_recovered")
                flags.set_flag(db, FLAG_LAST_ERROR, "")
                flags.set_flag(db, FLAG_CURRENT_STAGE, "stale_session_recovered")
                db.add(models.AuditLog(
                    actor="scheduler", action="agent_takeover_stale_session_recovered",
                    entity="agent_session", entity_id=active_session,
                    detail=json.dumps({"reason": "session_not_found", "provider_id": current.get("session_provider")}),
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
        provider = agent_providers.get_selected_provider(db)
        agent_providers.require_capability(provider, agent_providers.CAP_SCHEDULED_TAKEOVER)
        flags.set_flag(db, FLAG_CYCLE_ID, cycle_id)
        flags.set_flag(db, FLAG_CURRENT_STAGE, "provider_status")
        db.commit()
        display_timezone = workspace_time.get_timezone(db)
        title = f"Inbox Operation · {workspace_time.display(now, display_timezone)['local']}"
        takeover_token = secrets.token_urlsafe(32)
        # Publish the short-lived capability before prompt_async starts so the
        # first fast MCP call cannot race authorization persistence.
        flags.set_flag(db, FLAG_TOKEN_HASH, hashlib.sha256(takeover_token.encode("utf-8")).hexdigest())
        flags.set_flag(db, FLAG_TOKEN_EXPIRES, (now + timedelta(minutes=max(interval, 120))).isoformat())
        flags.set_flag(db, FLAG_CYCLE_HAS_ERRORS, "false")
        flags.set_flag(db, FLAG_LAST_ERROR, "")
        flags.set_flag(db, FLAG_LAST_SUCCESS_STAGE, "provider_status")
        flags.set_flag(db, FLAG_CURRENT_STAGE, "session_create")
        db.commit()
        created = provider.create_session(
            title=title,
            prompt=_operating_prompt(cycle_id, db),
            system=_operating_system_context(takeover_token),
        )
        session_id = str(created.get("id") or "").strip()
        workspace_id = str(created.get("workspace_id") or "").strip()
        if not session_id:
            raise agent_providers.AgentProviderError("agent_provider_protocol_error", "session_id_missing")
        flags.set_flag(db, FLAG_WORKSPACE, workspace_id)
        flags.set_flag(db, FLAG_SESSION_PROVIDER, provider.id)
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
            entity="agent_session", entity_id=session_id,
            detail=json.dumps({"cycle_id": cycle_id, "workspace_id": workspace_id, "interval_minutes": interval,
                               "started_at_utc": now.isoformat(), "display_timezone": display_timezone,
                               "started_at_display": workspace_time.display(now, display_timezone)["local"],
                               "provider_id": provider.id}),
        ))
        db.commit()
        return {"status": "running", "session_id": session_id, "workspace_id": workspace_id, "provider_id": provider.id}
    except Exception as exc:
        failed_stage = flags.get_flag(db, FLAG_CURRENT_STAGE)
        flags.set_flag(db, FLAG_LAST_STATUS, "failed")
        flags.set_flag(db, FLAG_LAST_ERROR, str(exc)[:500])
        flags.set_flag(db, FLAG_CYCLE_HAS_ERRORS, "true")
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
