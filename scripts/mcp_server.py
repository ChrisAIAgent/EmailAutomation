#!/usr/bin/env python3
"""Local stdio MCP bridge for the Email Automation HTTP API.

This module intentionally uses only the Python standard library so the bundled
``tools/python/python.exe`` can run it on a portable installation. It contains
no email, approval, scheduling, or send logic; every operation is delegated to
the loopback-only FastAPI service where the production safeguards live.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


API_BASE = os.environ.get("EMAIL_AUTOMATION_API_URL", "http://127.0.0.1:18000").rstrip("/")
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
SERVER_INFO = {"name": "email-automation", "version": "1.2.4"}
MCP_DIAGNOSTIC_LOG = Path(os.environ.get("EMAIL_AUTOMATION_DATA_DIR", str(WORKSPACE_ROOT))) / "logs" / "mcp-server.log"


class ApiRequestError(RuntimeError):
    """Structured, non-secret loopback failure for MCP clients."""

    def __init__(self, message: str, *, boundary: str, request_id: str, status: int | None = None):
        super().__init__(message)
        self.boundary = boundary
        self.request_id = request_id
        self.status = status

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": "api_request_failed",
            "message": str(self),
            "boundary": self.boundary,
            "request_id": self.request_id,
            "status": self.status,
            "result_unknown": self.boundary == "mcp_http_timeout",
            "retry_automatically": False,
        }


def _diagnostic(event: str, **fields: Any) -> None:
    """Best-effort local diagnostics; never write protocol output to stdout."""
    try:
        MCP_DIAGNOSTIC_LOG.parent.mkdir(parents=True, exist_ok=True)
        payload = {"event": event, "cwd": str(Path.cwd()), **fields}
        with MCP_DIAGNOSTIC_LOG.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _tool(name: str, description: str, properties: dict[str, Any] | None = None,
          required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": False,
        },
    }


TAKEOVER_CONTEXT_PROPERTIES = {
    "authorization_source": {"type": "string", "enum": ["agent_takeover"]},
    "takeover_token": {"type": "string"},
}

# Read failures otherwise have no write-operation telemetry path. These are the
# scheduled-cycle reads the operating prompt depends on; recording their failure
# means an idle TACWork session cannot be reported as a clean completion.
TAKEOVER_READ_FAILURE_STAGES = {
    "ea_takeover_status": "read_operating_state",
    "ea_dashboard": "read_operating_state",
    "ea_list_inbox": "read_inbox",
    "ea_daily_triage_status": "read_daily_triage",
    "ea_get_agent_run": "poll_agent_run",
}


TOOLS = [
    _tool("ea_takeover_status", "Read-only first-takeover summary for the Email Automation Workspace. Use on the first user message or whenever session context is uncertain. It checks service health, Gmail, AI configuration status, pause state, readiness, business counts and blockers. It never syncs Gmail, writes data, creates Drafts/Approvals or sends mail.", TAKEOVER_CONTEXT_PROPERTIES),
    _tool("ea_health", "Read service and Consumer health. Use before every write action."),
    _tool("ea_gmail_status", "Read Gmail connection/account state. Never exposes OAuth tokens."),
    _tool("ea_system_pause", "Read the global pause safety state."),
    _tool("ea_list_contacts", "List CRM contacts. Read-only.", {"query": {"type": "string", "description": "Optional URL query string without ?"}}),
    _tool("ea_list_campaigns", "List non-archived Campaigns. Read-only."),
    _tool("ea_list_approvals", "List Approvals for review. Read-only; pending is the default.", {"status": {"type": "string", "default": "pending"}, "kind": {"type": "string"}}),
    _tool("ea_revise_approval", "WRITE, NO SEND: Revise exactly one pending Approval and its existing Gmail Draft from an explicit user instruction. It never creates another Approval, never changes Inbox thread identity, and never confirms or sends mail.", {"approval_id": {"type": "integer", "minimum": 1}, "instruction": {"type": "string", "minLength": 1, "maxLength": 2000}, "editor_email": {"type": "string"}, "user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["approval_id", "instruction", "user_authorized"]),
    _tool("ea_list_automations", "List Global and Campaign Automations and their schedules. Read-only."),
    _tool("ea_dashboard", "Read readiness, metrics, Inbox stats and scheduler status. Read-only.", TAKEOVER_CONTEXT_PROPERTIES),
    _tool("ea_list_inbox", "List Inbox threads by effective category. human_review is not needs_reply.", {"category": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100}, **TAKEOVER_CONTEXT_PROPERTIES}),
    _tool("ea_generate_inbox_reply", "WRITE, NO SEND: Generate one eligible Inbox reply in its existing Gmail thread. It creates a Gmail Draft and pending Approval only after the existing human/contact and sales gates pass; it never confirms or sends mail.", {"thread_id": {"type": "integer", "minimum": 1}, "user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["thread_id", "user_authorized"]),
    _tool("ea_get_agent_run", "Read one Agent Run, including plan, timeline and status. Scheduled takeover sessions should include their capability so progress is traced.", {"run_id": {"type": "integer", "minimum": 1}, "authorization_source": {"type": "string", "enum": ["agent_takeover"]}, "takeover_token": {"type": "string"}}, ["run_id"]),
    _tool("ea_gmail_import_status", "READ: Check whether the one-time full mailbox import is required, running, paused or complete, including non-sensitive progress counters."),
    _tool("ea_start_gmail_import", "WRITE, NO SEND: Start the one-time full mailbox import after explicit user authorization. Imports all accessible mail except Spam and Trash into local data; it does not sort, create Contacts/Drafts/Approvals or send.", {"user_authorized": {"type": "boolean"}}, ["user_authorized"]),
    _tool("ea_control_gmail_import", "WRITE, NO SEND: Pause, resume or cancel the current full mailbox import after explicit user authorization.", {"run_id": {"type": "integer", "minimum": 1}, "action": {"type": "string", "enum": ["pause", "resume", "cancel"]}, "user_authorized": {"type": "boolean"}}, ["run_id", "action", "user_authorized"]),
    _tool("ea_initial_triage_status", "READ: Check the one-time first-history Inbox triage after mailbox import, including progress and non-sensitive classification counters."),
    _tool("ea_start_initial_triage", "WRITE, NO SEND: Start the one-time first-history Inbox triage after the full mailbox import is complete. It classifies a frozen local snapshot in 50-thread batches; it never creates Drafts, Approvals or sends mail.", {"user_authorized": {"type": "boolean"}}, ["user_authorized"]),
    _tool("ea_control_initial_triage", "WRITE, NO SEND: Pause, resume, cancel or retry failed items in the first-history Inbox triage after explicit user authorization.", {"run_id": {"type": "integer", "minimum": 1}, "action": {"type": "string", "enum": ["pause", "resume", "cancel", "retry_failed"]}, "user_authorized": {"type": "boolean"}}, ["run_id", "action", "user_authorized"]),
    _tool("ea_daily_triage_status", "READ: Check the current or latest daily incremental triage Run, its fixed snapshot, progress and results.", TAKEOVER_CONTEXT_PROPERTIES, []),
    _tool("ea_recent_triage_result", "READ: Get the latest first-history or daily Inbox triage summary and its completed/progress time.", {}, []),
    _tool("ea_start_daily_triage", "WRITE, NO SEND: Freeze all currently untriaged newly synced or changed conversations into one resumable daily triage Run. It processes every snapshot item in safe 50-thread worker batches; it never drafts or sends mail.", {"user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["user_authorized"]),
    _tool("ea_control_daily_triage", "WRITE, NO SEND: Pause, resume, cancel or retry failed items in a daily incremental triage Run.", {"run_id": {"type": "integer", "minimum": 1}, "action": {"type": "string", "enum": ["pause", "resume", "cancel", "retry_failed"]}, "user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["run_id", "action", "user_authorized"]),
    _tool("ea_sync_gmail", "WRITE: After the first full import is complete, pull only Gmail History changes since the last successful cursor. Requires explicit user authorization or a valid scheduled Agent Takeover capability; never sends mail.", {"full_scan": {"type": "boolean", "default": False, "description": "Deprecated compatibility flag; use ea_start_gmail_import."}, "user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["user_authorized"]),
    _tool("ea_sort_inbox", "Deprecated compatibility alias for ea_start_daily_triage. It creates a resumable snapshot Run; 50 is only the worker batch size. Requires explicit user authorization or a valid Agent Takeover capability; never sends mail.", {"user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["user_authorized"]),
    _tool("ea_create_contact", "WRITE: Create one confirmed-human Contact through the API. Never infer identity from forwarded/system mail.", {"contact": {"type": "object"}, "user_authorized": {"type": "boolean"}}, ["contact", "user_authorized"]),
    _tool("ea_import_contacts", "WRITE: Preview or import a UTF-8 CSV/XLSX file located inside this Workspace. Confirm=false is preview-only.", {"path": {"type": "string"}, "confirm": {"type": "boolean", "default": False}, "user_authorized": {"type": "boolean"}}, ["path", "user_authorized"]),
    _tool("ea_create_campaign", "WRITE: Create a Campaign configuration; does not generate or send mail. An empty Campaign list is normal, not a missing configuration file: with complete user-provided details and authorization, call this tool directly. A valid Agent Takeover capability may create it as part of full operating authority.", {"campaign": {"type": "object"}, "user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["campaign", "user_authorized"]),
    _tool("ea_get_campaign", "READ: Get one Campaign by id, including its current configuration and lifecycle status.", {"campaign_id": {"type": "integer", "minimum": 1}}, ["campaign_id"]),
    _tool("ea_list_campaign_members", "READ: List active or historical members of one Campaign. Use before and after membership changes; never searches source code for this operation.", {"campaign_id": {"type": "integer", "minimum": 1}, "include_removed": {"type": "boolean", "default": False}}, ["campaign_id"]),
    _tool("ea_add_campaign_contacts", "WRITE, NO SEND: Add or re-add existing Contacts to one Campaign. The server preserves history, skips suppressed/ineligible Contacts, and returns exact added/skipped results.", {"campaign_id": {"type": "integer", "minimum": 1}, "contact_ids": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1}, "user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["campaign_id", "contact_ids", "user_authorized"]),
    _tool("ea_remove_campaign_contact", "DESTRUCTIVE WRITE, NO SEND: Remove one Contact only after the operator explicitly asks to remove that Contact from this Campaign. It expires unsent pending Approvals and cancels Drafts/follow-ups. Never use it to recover a failed copy revision, reset outreach_generated, or regenerate an email.", {"campaign_id": {"type": "integer", "minimum": 1}, "contact_id": {"type": "integer", "minimum": 1}, "confirmed_removal": {"type": "boolean", "description": "Must be true only after a direct operator request to remove this Campaign member; never infer it from a revision or generation failure."}, "user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["campaign_id", "contact_id", "confirmed_removal", "user_authorized"]),
    _tool("ea_update_campaign", "WRITE, NO SEND: Replace one Campaign's configuration with a complete valid Campaign payload. Read it first so unchanged fields are preserved.", {"campaign_id": {"type": "integer", "minimum": 1}, "campaign": {"type": "object"}, "user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["campaign_id", "campaign", "user_authorized"]),
    _tool("ea_generate_campaign_outreach", "WRITE, NO SEND: Generate first-email Drafts and pending Approvals for currently queued active Campaign members. Requires explicit authorization; it never proves a send. A timeout is result_unknown: reconcile using generation status, Approvals, and members; never retry automatically. A new call after terminal failed/partial needs new explicit user authorization.", {"campaign_id": {"type": "integer", "minimum": 1}, "user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["campaign_id", "user_authorized"]),
    _tool("ea_get_campaign_generation", "READ: Get latest Campaign generation status, including partial successes and retryable contact failures. After timeout: running means wait/reconcile; terminal failed/partial may be retried only with new explicit user authorization after checking pending Approvals and queued active members.", {"campaign_id": {"type": "integer", "minimum": 1}}, ["campaign_id"]),
    _tool("ea_start_campaign", "WRITE, NO SEND: Mark one Campaign active. Sending still requires the existing Approval, policy, and Gmail checks.", {"campaign_id": {"type": "integer", "minimum": 1}, "user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["campaign_id", "user_authorized"]),
    _tool("ea_pause_campaign", "WRITE, NO SEND: Pause one Campaign without deleting its history.", {"campaign_id": {"type": "integer", "minimum": 1}, "user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["campaign_id", "user_authorized"]),
    _tool("ea_stop_campaign", "WRITE, NO SEND: Stop one Campaign and cancel its scheduled follow-ups while preserving history.", {"campaign_id": {"type": "integer", "minimum": 1}, "user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["campaign_id", "user_authorized"]),
    _tool("ea_get_automation", "READ: Get one Automation and its recent Runs.", {"automation_id": {"type": "integer", "minimum": 1}}, ["automation_id"]),
    _tool("ea_generate_automation_plan", "WRITE, NO SEND: Generate a conservative structured Automation plan from a prompt. It creates no Automation, Draft, Approval, or send.", {"prompt": {"type": "string", "minLength": 1}, "campaign_id": {"type": "integer", "minimum": 1}, "scope": {"type": "string", "enum": ["campaign", "global"], "default": "campaign"}, "user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["prompt", "user_authorized"]),
    _tool("ea_create_automation", "WRITE, NO SEND: Create one Campaign or Global Automation from an explicit structured plan. It remains subject to enablement, approval mode, and all server safety gates.", {"prompt": {"type": "string", "minLength": 1}, "campaign_id": {"type": "integer", "minimum": 1}, "scope": {"type": "string", "enum": ["campaign", "global"], "default": "campaign"}, "plan": {"type": "object"}, "name": {"type": "string"}, "user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["prompt", "plan", "user_authorized"]),
    _tool("ea_enable_automation", "WRITE, NO SEND: Enable one existing Automation. This changes scheduling configuration only; a later Run still performs policy checks.", {"automation_id": {"type": "integer", "minimum": 1}, "user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["automation_id", "user_authorized"]),
    _tool("ea_pause_automation", "WRITE, NO SEND: Pause one existing Automation without deleting history.", {"automation_id": {"type": "integer", "minimum": 1}, "user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["automation_id", "user_authorized"]),
    _tool("ea_schedule_automation", "WRITE, NO SEND: Set one Automation's cadence in whole minutes (1-1440).", {"automation_id": {"type": "integer", "minimum": 1}, "tick_interval_minutes": {"type": "integer", "minimum": 1, "maximum": 1440}, "user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["automation_id", "tick_interval_minutes", "user_authorized"]),
    _tool("ea_start_agent_run", "HIGH IMPACT: Start an owned enabled Automation Run. A scheduled Agent Takeover capability may use full operating authority; server safety gates still apply.", {"automation_id": {"type": "integer", "minimum": 1}, "mode": {"type": "string", "enum": ["full_auto", "semi_auto"]}, "user_authorized": {"type": "boolean"}, "authorization_source": {"type": "string", "enum": ["user", "agent_takeover"]}, "takeover_token": {"type": "string"}}, ["automation_id", "mode", "user_authorized"]),
    _tool("ea_confirm_run", "SEND-CAPABLE: Confirm the exact frozen semi-auto plan. May send mail through server safeguards. Use only after explicit confirmation of recipients and full content.", {"run_id": {"type": "integer", "minimum": 1}, "confirmed_by": {"type": "string"}, "user_authorized": {"type": "boolean"}}, ["run_id", "user_authorized"]),
    _tool("ea_cancel_run", "WRITE, NO SEND: Cancel one exact prepared semi-auto Run. Does not generate or send mail.", {"run_id": {"type": "integer", "minimum": 1}, "user_authorized": {"type": "boolean"}}, ["run_id", "user_authorized"]),
    _tool("ea_invalidate_approval", "DESTRUCTIVE WRITE, NO SEND: Expire one pending Approval only when the operator explicitly says it is obsolete or already sent. It cancels the unsent Draft. Never use it because a revision, Draft update, or generation failed.", {"approval_id": {"type": "integer", "minimum": 1}, "reason": {"type": "string"}, "reason_category": {"type": "string", "enum": ["obsolete", "already_sent"], "description": "The direct operator reason for invalidation; revision or generation failure is not valid."}, "editor_email": {"type": "string"}, "user_authorized": {"type": "boolean"}}, ["approval_id", "reason", "reason_category", "user_authorized"]),
    _tool("ea_create_knowledge", "WRITE: Create draft or published knowledge. Published content may influence future replies.", {"title": {"type": "string"}, "category": {"type": "string", "default": "general"}, "content": {"type": "string"}, "publish": {"type": "boolean", "default": False}, "user_authorized": {"type": "boolean"}}, ["title", "content", "user_authorized"]),
    # Knowledge self-management (Agent Native): the Agent can fully manage its own KB lifecycle.
    _tool("ea_list_knowledge", "READ: List current knowledge documents.", {}, []),
    _tool("ea_get_knowledge", "READ: Get one knowledge document by id.", {"document_id": {"type": "integer", "minimum": 1}}, ["document_id"]),
    _tool("ea_search_knowledge", "READ: Search knowledge by query string.", {"query": {"type": "string"}}, ["query"]),
    _tool("ea_update_knowledge", "WRITE: Update a knowledge document's title/category/content.", {"document_id": {"type": "integer", "minimum": 1}, "title": {"type": "string"}, "category": {"type": "string"}, "content": {"type": "string"}, "user_authorized": {"type": "boolean"}}, ["document_id", "user_authorized"]),
    _tool("ea_publish_knowledge", "WRITE: Publish a knowledge document so its content can influence future replies.", {"document_id": {"type": "integer", "minimum": 1}, "user_authorized": {"type": "boolean"}}, ["document_id", "user_authorized"]),
    _tool("ea_disable_knowledge", "WRITE: Disable a knowledge document (stops influencing replies; reversible).", {"document_id": {"type": "integer", "minimum": 1}, "user_authorized": {"type": "boolean"}}, ["document_id", "user_authorized"]),
    _tool("ea_delete_knowledge", "DESTRUCTIVE WRITE: Permanently delete one knowledge document. This cannot be undone and always requires explicit user authorization.", {"document_id": {"type": "integer", "minimum": 1}, "user_authorized": {"type": "boolean"}}, ["document_id", "user_authorized"]),
    # Agent Profile self-configuration (Agent Native): the Agent can read and first-time configure its own identity.
    _tool("ea_get_profile", "READ: Get the current Agent identity/behavior Profile without creating a default Profile when none exists."),
    _tool("ea_configure_profile", "WRITE: Set the Agent identity/behavior Profile from scratch (full payload). Changes the Agent's name, company, signature, tone, language policy, forbidden claims, unknown-answer policy, and optional approval mode. First read ea_get_profile to get current defaults.", {"agent_name": {"type": "string"}, "company_name": {"type": "string"}, "role": {"type": "string"}, "tone": {"type": "string"}, "language_policy": {"type": "string", "enum": ["match_customer", "chinese", "english"]}, "signature_text": {"type": "string"}, "forbidden_claims": {"type": "string"}, "unknown_answer_policy": {"type": "string"}, "allow_campaign_override": {"type": "boolean"}, "approval_mode": {"type": "string", "enum": ["human_review", "agent_review"]}, "is_active": {"type": "boolean"}, "user_authorized": {"type": "boolean"}}, ["agent_name", "company_name", "role", "tone", "language_policy", "signature_text", "unknown_answer_policy", "user_authorized"]),
    _tool("ea_set_approval_mode", "WRITE, HIGH IMPACT: Set the Workspace-wide send authority. human_review forces future Agent runs to freeze for human confirmation; agent_review permits configured full_auto runs only after all server safety checks. Requires explicit user authorization.", {"approval_mode": {"type": "string", "enum": ["human_review", "agent_review"]}, "user_authorized": {"type": "boolean"}}, ["approval_mode", "user_authorized"]),
    _tool("ea_agent_report", "Read a live, truthful operations report assembled from current API state. Does not run or send anything."),
]


def _request(method: str, path: str, body: Any = None, timeout: int = 90,
             headers: dict[str, str] | None = None) -> Any:
    url = API_BASE + path
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request_headers = {"Accept": "application/json"}
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    request_id = request_headers.setdefault("X-Request-ID", uuid.uuid4().hex[:12])
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {"ok": True}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ApiRequestError(
            f"Email Automation API {exc.code}: {detail[:2000]}",
            boundary="email_automation_api", request_id=request_id, status=exc.code,
        ) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ApiRequestError(
            f"Email Automation API request timed out after {timeout}s; the server result is unknown. Check the Campaign generation status before trying again.",
            boundary="mcp_http_timeout", request_id=request_id,
        ) from exc
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        boundary = "mcp_http_timeout" if "timed out" in reason.lower() else "mcp_http_transport"
        raise ApiRequestError(
            f"Email Automation API unavailable at {API_BASE}: {reason}",
            boundary=boundary, request_id=request_id,
        ) from exc


def _authorized(args: dict[str, Any], operation: str | None = None) -> None:
    if args.get("user_authorized") is not True:
        raise ValueError("user_authorized=true is required after explicit user authorization")
    if args.get("authorization_source") == "agent_takeover":
        if not operation:
            raise ValueError("Agent Takeover does not authorize this operation")
        takeover_request = {
            "token": args.get("takeover_token") or "",
            "operation": operation,
        }
        for key in ("automation_id", "campaign_id", "thread_id", "approval_id"):
            if args.get(key) is not None:
                takeover_request[key] = int(args[key])
        _request("POST", "/api/agent-takeover/authorize", takeover_request)


def _takeover_telemetry(args: dict[str, Any], stage: str, status: str, detail: dict[str, Any] | None = None) -> None:
    if args.get("authorization_source") != "agent_takeover":
        return
    _request("POST", "/api/agent-takeover/telemetry", {
        "token": args.get("takeover_token") or "",
        "stage": stage,
        "status": status,
        "detail": detail or {},
    })


def _takeover_operation(args: dict[str, Any], stage: str, operation: str, action) -> Any:
    _authorized(args, operation)
    try:
        _takeover_telemetry(args, stage, "started")
    except Exception:
        # Observability must not become an availability dependency for the
        # authorized operational action.
        pass
    try:
        result = action()
    except Exception as exc:
        try:
            _takeover_telemetry(args, stage, "failed", {"error": str(exc)[:300]})
        except Exception:
            pass
        raise
    safe_detail = {}
    if isinstance(result, dict):
        for key in ("threads", "messages", "sorted", "failed", "run_id", "status", "mode", "added", "generated", "automation_id", "campaign_id", "approval_id", "draft_id", "revision_applied"):
            if key in result:
                safe_detail[key] = result[key]
    try:
        _takeover_telemetry(args, stage, "success", safe_detail)
    except Exception:
        pass
    return result


def _count(value: Any) -> int:
    """Count common list response shapes without exposing their content."""
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in ("items", "results", "data"):
            if isinstance(value.get(key), list):
                return len(value[key])
    return 0


def _takeover_status() -> dict[str, Any]:
    """Aggregate safe operational state for a first, strictly read-only takeover."""
    health = _request("GET", "/api/health")
    gmail = _request("GET", "/api/gmail/status")
    pause = _request("GET", "/api/system/pause")
    agent_health = _request("GET", "/api/agent/health")
    profile = _request("GET", "/api/agent-profile?create_if_missing=false")
    readiness = _request("GET", "/api/dashboard/readiness")
    metrics = _request("GET", "/api/dashboard/metrics")
    inbox_stats = _request("GET", "/api/inbox/stats")
    contacts = _request("GET", "/api/contacts")
    campaigns = _request("GET", "/api/campaigns")
    automations = _request("GET", "/api/automation")
    approvals = _request("GET", "/api/approvals?status=pending")
    scheduler = _request("GET", "/api/automation/scheduler/status")
    takeover = _request("GET", "/api/agent-takeover")

    if isinstance(agent_health, list):
        langgraph = next((item for item in agent_health if isinstance(item, dict) and item.get("agent") == "langgraph"), {})
    elif isinstance(agent_health, dict):
        # Compatibility with the short-lived object-shaped health response.
        candidate = agent_health.get("langgraph", agent_health)
        langgraph = candidate if isinstance(candidate, dict) else {}
    else:
        langgraph = {}
    ai_configured = bool(
        langgraph.get("configured")
        or (metrics.get("langgraph_configured") if isinstance(metrics, dict) else False)
    )
    gmail_connected = bool(gmail.get("connected")) if isinstance(gmail, dict) else False
    paused = bool(pause.get("global_pause")) if isinstance(pause, dict) else False
    stats = inbox_stats if isinstance(inbox_stats, dict) else {}
    frozen = readiness.get("awaiting_confirmation_runs", []) if isinstance(readiness, dict) else []
    blockers = list(readiness.get("blockers", [])) if isinstance(readiness, dict) else []
    if not ai_configured and "email_ai_not_configured" not in blockers:
        blockers.append("email_ai_not_configured")
    if gmail_connected and not bool(gmail.get("initial_import_completed")) and "initial_import_required" not in blockers:
        blockers.append("initial_import_required")

    counts = {
        "contacts": _count(contacts),
        "campaigns": _count(campaigns),
        "automations": _count(automations),
        "pending_approvals": _count(approvals),
        # ``triage_review`` is an already-analysed review outcome whose legacy
        # display category is ``unsorted``.  The operational count must use the
        # API's explicit unprocessed metric, matching the Inbox action button.
        "unsorted_threads": int(stats.get("unprocessed", 0) or 0),
        "untriaged_threads": int(stats.get("unprocessed", 0) or 0),
        "human_review_threads": int(stats.get("human_review", 0) or 0),
        "needs_reply_threads": int(stats.get("needs_reply", 0) or 0),
    }
    recommended: list[str] = []
    if not gmail_connected:
        recommended.append("connect_gmail_in_browser")
    elif not bool(gmail.get("initial_import_completed")):
        recommended.append("explain_and_request_first_mail_import_authorization")
    if not ai_configured:
        recommended.append("configure_email_ai")
    if not bool(profile.get("configured")):
        recommended.append("configure_agent_profile")
    for key, action in (
        ("human_review_threads", "review_human_review"),
        ("needs_reply_threads", "review_needs_reply"),
        ("pending_approvals", "review_pending_approvals"),
        ("unsorted_threads", "authorize_sync_and_triage_inbox"),
    ):
        if counts[key] > 0:
            recommended.append(action)
    if not recommended:
        recommended.append("review_dashboard_and_business_goal")

    return {
        "workspace_mode": "production",
        "service": {
            "status": health.get("status") if isinstance(health, dict) else None,
            "consumer_healthy": bool((health.get("consumer") or {}).get("healthy")) if isinstance(health, dict) else False,
            "scheduler_status": scheduler.get("status") if isinstance(scheduler, dict) else None,
        },
        "gmail": {
            "connected": gmail_connected,
            "email": gmail.get("email") if isinstance(gmail, dict) else None,
            "initial_import_completed": bool(gmail.get("initial_import_completed")) if isinstance(gmail, dict) else False,
            "sync_state": gmail.get("sync_state") if isinstance(gmail, dict) else None,
        },
        "ai": {
            "configured": ai_configured,
            "langgraph_reachable": bool(langgraph.get("reachable")),
            "provider_reachable": None,
            "provider_check": "use_email_ai_config_test",
            "detail": langgraph.get("detail") or None,
            "latency_ms": langgraph.get("latency_ms"),
            "config": health.get("llm_config") if isinstance(health, dict) else None,
        },
        "profile": {
            "configured": bool(profile.get("configured")),
            "approval_mode": profile.get("approval_mode") or "human_review",
        },
        "pause": {"global_pause": paused},
        "agent_takeover": takeover,
        "readiness": {
            "status": readiness.get("status") if isinstance(readiness, dict) else None,
            "next_action": readiness.get("next_action") if isinstance(readiness, dict) else None,
            "real_send": bool(readiness.get("real_send")) if isinstance(readiness, dict) else False,
            "frozen_runs": len(frozen) if isinstance(frozen, list) else 0,
        },
        "counts": counts,
        "blockers": blockers,
        "recommended_next_actions": recommended,
    }


def _multipart_file(path: Path) -> tuple[bytes, str]:
    boundary = "----EmailAutomationMCP" + uuid.uuid4().hex
    content_type = "application/octet-stream"
    payload = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8") + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("ascii")
    return payload, boundary


def _upload_contacts(args: dict[str, Any]) -> Any:
    _authorized(args)
    candidate = (WORKSPACE_ROOT / str(args["path"])).resolve()
    try:
        candidate.relative_to(WORKSPACE_ROOT)
    except ValueError as exc:
        raise ValueError("contact import path must stay inside the Email Automation Workspace") from exc
    if not candidate.is_file() or candidate.suffix.lower() not in {".csv", ".xlsx"}:
        raise ValueError("contact import requires an existing Workspace .csv or .xlsx file")
    payload, boundary = _multipart_file(candidate)
    query = urllib.parse.urlencode({"confirm": str(bool(args.get("confirm", False))).lower()})
    req = urllib.request.Request(
        f"{API_BASE}/api/contacts/import?{query}", data=payload, method="POST",
        headers={"Accept": "application/json", "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Email Automation API {exc.code}: {exc.read().decode('utf-8', errors='replace')[:2000]}") from exc


def call_tool(name: str, args: dict[str, Any]) -> Any:
    if name == "ea_takeover_status": return _takeover_status()
    if name == "ea_health": return _request("GET", "/api/health")
    if name == "ea_gmail_status": return _request("GET", "/api/gmail/status")
    if name == "ea_system_pause": return _request("GET", "/api/system/pause")
    if name == "ea_list_contacts": return _request("GET", "/api/contacts" + (("?" + args["query"]) if args.get("query") else ""))
    if name == "ea_list_campaigns": return _request("GET", "/api/campaigns")
    if name == "ea_list_approvals":
        query = urllib.parse.urlencode({k: v for k, v in {"status": args.get("status", "pending"), "kind": args.get("kind")}.items() if v})
        return _request("GET", "/api/approvals?" + query)
    if name == "ea_revise_approval":
        body = {"instruction": args["instruction"]}
        if args.get("editor_email"):
            body["editor_email"] = args["editor_email"]
        return _takeover_operation(
            args, "revise_approval", "revise_approval",
            lambda: _request("POST", f"/api/approvals/{int(args['approval_id'])}/revise", body),
        )
    if name == "ea_list_automations": return _request("GET", "/api/automation")
    if name == "ea_dashboard":
        return {key: _request("GET", path) for key, path in {
            "readiness": "/api/dashboard/readiness", "metrics": "/api/dashboard/metrics",
            "inbox": "/api/inbox/stats", "scheduler": "/api/automation/scheduler/status",
        }.items()}
    if name == "ea_list_inbox":
        query = urllib.parse.urlencode({k: v for k, v in {"category": args.get("category"), "limit": args.get("limit", 100)}.items() if v is not None})
        return _request("GET", "/api/inbox/threads?" + query)
    if name == "ea_generate_inbox_reply":
        return _takeover_operation(args, "generate_inbox_reply", "generate_inbox_reply", lambda: _request("POST", f"/api/inbox/threads/{int(args['thread_id'])}/generate-reply"))
    if name == "ea_get_agent_run":
        result = _request("GET", f"/api/agent-runs/{int(args['run_id'])}")
        if args.get("authorization_source") == "agent_takeover":
            _takeover_telemetry(args, "poll_agent_run", "success", {
                "run_id": int(args["run_id"]), "status": result.get("status") if isinstance(result, dict) else None,
            })
        return result
    if name == "ea_gmail_import_status":
        return _request("GET", "/api/gmail/imports/current")
    if name == "ea_start_gmail_import":
        _authorized(args)
        return _request("POST", "/api/gmail/imports")
    if name == "ea_control_gmail_import":
        _authorized(args)
        return _request("POST", f"/api/gmail/imports/{int(args['run_id'])}/{args['action']}")
    if name == "ea_initial_triage_status":
        return _request("GET", "/api/inbox/initial-triage/current")
    if name == "ea_start_initial_triage":
        _authorized(args)
        return _request("POST", "/api/inbox/initial-triage")
    if name == "ea_control_initial_triage":
        _authorized(args)
        suffix = "retry-failed" if args["action"] == "retry_failed" else args["action"]
        return _request("POST", f"/api/inbox/initial-triage/{int(args['run_id'])}/{suffix}")
    if name == "ea_daily_triage_status":
        return _request("GET", "/api/inbox/daily-triage/current")
    if name == "ea_recent_triage_result":
        return _request("GET", "/api/inbox/triage/recent")
    if name == "ea_start_daily_triage":
        return _takeover_operation(args, "start_daily_triage", "start_daily_triage", lambda: _request("POST", "/api/inbox/daily-triage"))
    if name == "ea_control_daily_triage":
        suffix = "retry-failed" if args["action"] == "retry_failed" else args["action"]
        return _takeover_operation(args, "control_daily_triage", "control_daily_triage", lambda: _request("POST", f"/api/inbox/daily-triage/{int(args['run_id'])}/{suffix}"))
    if name == "ea_sync_gmail":
        query = "?full_scan=true" if args.get("full_scan") else ""
        return _takeover_operation(args, "sync_gmail", "sync_gmail", lambda: _request("POST", "/api/gmail/sync" + query))
    if name == "ea_sort_inbox":
        return _takeover_operation(args, "start_daily_triage", "start_daily_triage", lambda: _request("POST", "/api/inbox/daily-triage"))
    if name == "ea_create_contact": _authorized(args); return _request("POST", "/api/contacts", args["contact"])
    if name == "ea_import_contacts": return _upload_contacts(args)
    if name == "ea_create_campaign":
        return _takeover_operation(args, "create_campaign", "create_campaign", lambda: _request("POST", "/api/campaigns", args["campaign"]))
    if name == "ea_get_campaign":
        return _request("GET", f"/api/campaigns/{int(args['campaign_id'])}")
    if name == "ea_list_campaign_members":
        query = urllib.parse.urlencode({"include_removed": str(bool(args.get("include_removed", False))).lower()})
        return _request("GET", f"/api/campaigns/{int(args['campaign_id'])}/contacts?{query}")
    if name == "ea_add_campaign_contacts":
        return _takeover_operation(args, "add_campaign_contacts", "add_campaign_contacts", lambda: _request("POST", f"/api/campaigns/{int(args['campaign_id'])}/contacts", {"contact_ids": args["contact_ids"]}))
    if name == "ea_remove_campaign_contact":
        if args.get("confirmed_removal") is not True:
            raise ValueError("confirmed_removal=true is required after a direct operator request to remove this Campaign member")
        return _takeover_operation(args, "remove_campaign_contact", "remove_campaign_contact", lambda: _request("DELETE", f"/api/campaigns/{int(args['campaign_id'])}/contacts/{int(args['contact_id'])}"))
    if name == "ea_update_campaign":
        return _takeover_operation(args, "update_campaign", "update_campaign", lambda: _request("PUT", f"/api/campaigns/{int(args['campaign_id'])}", args["campaign"]))
    if name == "ea_generate_campaign_outreach":
        return _takeover_operation(args, "generate_campaign_outreach", "generate_campaign_outreach", lambda: _request("POST", f"/api/campaigns/{int(args['campaign_id'])}/generate"))
    if name == "ea_get_campaign_generation":
        return _request("GET", f"/api/campaigns/{int(args['campaign_id'])}/generation-status")
    campaign_actions = {
        "ea_start_campaign": "start_campaign",
        "ea_pause_campaign": "pause_campaign",
        "ea_stop_campaign": "stop_campaign",
    }
    if name in campaign_actions:
        operation = campaign_actions[name]
        action = operation.removesuffix("_campaign")
        return _takeover_operation(args, operation, operation, lambda: _request("POST", f"/api/campaigns/{int(args['campaign_id'])}/{action}"))
    if name == "ea_get_automation":
        return _request("GET", f"/api/automation/{int(args['automation_id'])}")
    if name == "ea_generate_automation_plan":
        body = {key: args[key] for key in ("prompt", "campaign_id", "scope") if key in args}
        return _takeover_operation(args, "generate_automation_plan", "generate_automation_plan", lambda: _request("POST", "/api/automation/generate", body))
    if name == "ea_create_automation":
        body = {key: args[key] for key in ("prompt", "campaign_id", "scope", "plan", "name") if key in args}
        return _takeover_operation(args, "create_automation", "create_automation", lambda: _request("POST", "/api/automation", body))
    automation_actions = {
        "ea_enable_automation": "enable_automation",
        "ea_pause_automation": "pause_automation",
    }
    if name in automation_actions:
        operation = automation_actions[name]
        action = operation.removesuffix("_automation")
        return _takeover_operation(args, operation, operation, lambda: _request("POST", f"/api/automation/{int(args['automation_id'])}/{action}"))
    if name == "ea_schedule_automation":
        return _takeover_operation(args, "schedule_automation", "schedule_automation", lambda: _request("POST", f"/api/automation/{int(args['automation_id'])}/schedule", {"tick_interval_minutes": args["tick_interval_minutes"]}))
    if name == "ea_start_agent_run":
        if args.get("authorization_source") == "agent_takeover":
            return _takeover_operation(args, "start_agent_run", "start_agent_run", lambda: _request("POST", "/api/agent-runs", {"automation_id": args["automation_id"], "mode": args["mode"]}))
        _authorized(args); return _request("POST", "/api/agent-runs", {"automation_id": args["automation_id"], "mode": args["mode"]})
    if name == "ea_confirm_run": _authorized(args); return _request("POST", f"/api/agent-runs/{int(args['run_id'])}/confirm", {"confirmed_by": args.get("confirmed_by")})
    if name == "ea_cancel_run": _authorized(args); return _request("POST", f"/api/agent-runs/{int(args['run_id'])}/cancel")
    if name == "ea_invalidate_approval":
        if args.get("reason_category") not in {"obsolete", "already_sent"}:
            raise ValueError("reason_category must be obsolete or already_sent; revision or generation failure must preserve the pending Approval")
        _authorized(args); return _request("POST", f"/api/approvals/{int(args['approval_id'])}/invalidate", {"reason": args["reason"], "editor_email": args.get("editor_email")})
    if name == "ea_create_knowledge":
        _authorized(args); return _request("POST", "/api/knowledge", {"title": args["title"], "category": args.get("category", "general"), "content": args["content"], "publish": bool(args.get("publish", False))})
    if name == "ea_list_knowledge":
        return _request("GET", "/api/knowledge")
    if name == "ea_get_knowledge":
        return _request("GET", f"/api/knowledge/{int(args['document_id'])}")
    if name == "ea_search_knowledge":
        return _request("POST", "/api/knowledge/search", {"query": args["query"]})
    if name == "ea_update_knowledge":
        _authorized(args)
        body = {k: args[k] for k in ("title", "category", "content") if k in args}
        return _request("PUT", f"/api/knowledge/{int(args['document_id'])}", body)
    if name == "ea_publish_knowledge":
        _authorized(args); return _request("POST", f"/api/knowledge/{int(args['document_id'])}/publish")
    if name == "ea_disable_knowledge":
        _authorized(args); return _request("POST", f"/api/knowledge/{int(args['document_id'])}/disable")
    if name == "ea_delete_knowledge":
        _authorized(args); return _request("DELETE", f"/api/knowledge/{int(args['document_id'])}")
    if name == "ea_get_profile":
        return _request("GET", "/api/agent-profile?create_if_missing=false")
    if name == "ea_configure_profile":
        _authorized(args)
        keys = ("agent_name", "company_name", "role", "tone", "language_policy", "signature_text",
                "forbidden_claims", "unknown_answer_policy", "allow_campaign_override", "approval_mode", "is_active")
        body = {k: args[k] for k in keys if k in args}
        return _request("PUT", "/api/agent-profile?actor=agent", body)
    if name == "ea_set_approval_mode":
        _authorized(args)
        return _request("PUT", "/api/agent-profile/approval-mode?actor=agent", {"approval_mode": args["approval_mode"]})
    if name == "ea_agent_report":
        return {key: _request("GET", path) for key, path in {
            "health": "/api/health", "gmail": "/api/gmail/status", "pause": "/api/system/pause",
            "readiness": "/api/dashboard/readiness", "metrics": "/api/dashboard/metrics",
            "inbox": "/api/inbox/stats", "automations": "/api/automation", "pending_approvals": "/api/approvals?status=pending",
        }.items()}
    raise ValueError(f"unknown tool: {name}")


def _result(request_id: Any, value: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method, request_id = message.get("method"), message.get("id")
    if method == "initialize":
        return _result(request_id, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO})
    if method == "notifications/initialized": return None
    if method == "ping": return _result(request_id, {})
    if method == "tools/list": return _result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            value = call_tool(str(params.get("name", "")), params.get("arguments") or {})
            # MCP requires `structuredContent` to be a record/object. List-returning
            # tools (e.g. ea_list_contacts) would otherwise emit an array and the
            # client rejects with "expected record, received array".
            structured = value if isinstance(value, dict) else {"result": value}
            return _result(request_id, {
                "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}],
                "structuredContent": structured,
            })
        except Exception as exc:
            # Scheduled read tools do not pass through _takeover_operation. If
            # one fails, persist that fact before returning the original MCP
            # error so the scheduler can surface completed_with_errors rather
            # than a misleading clean completion. Telemetry is best-effort and
            # must never replace or hide the actual tool error.
            stage = TAKEOVER_READ_FAILURE_STAGES.get(str(params.get("name", "")))
            if stage:
                try:
                    _takeover_telemetry(params.get("arguments") or {}, stage, "failed", {"error": str(exc)[:300]})
                except Exception:
                    pass
            _diagnostic("tool_error", tool=str(params.get("name", "")), error=str(exc)[:300])
            structured_error = exc.as_dict() if isinstance(exc, ApiRequestError) else {"error": str(exc)}
            return _result(request_id, {
                "content": [{"type": "text", "text": str(exc)}],
                "structuredContent": structured_error,
                "isError": True,
            })
    if request_id is None: return None
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> None:
    _diagnostic("started", executable=sys.executable)
    for line in sys.stdin.buffer:
        if not line.strip(): continue
        try:
            response = handle(json.loads(line.decode("utf-8")))
        except Exception as exc:
            _diagnostic("protocol_error", error=str(exc)[:300])
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
