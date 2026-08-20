"""OpenClaw adapter.

OpenClaw is treated as an EXTERNAL agent we do not control. We:
  1. Translate the unified input into an OpenClaw task (transport-specific).
  2. Send it via the configured transport (http | webhook | cli | mcp).
  3. Strictly validate the response against the AgentDecision schema.
  4. Allow ONE format-repair attempt on invalid output; persist failure otherwise.

We NEVER emit random/fake results. If OpenClaw is unconfigured or unreachable,
health_check reports it and the methods return None (compare mode then simply
has no shadow result — primary still works).
"""
from __future__ import annotations

import json
import logging
import secrets
import subprocess
import time
from abc import ABC, abstractmethod
from typing import Optional

import httpx

from .. import models
from ..config import get_settings, is_openclaw_configured
from ..schemas import (
    AgentDecision,
    AgentHealth,
    EmailProposal,
    AnalyzeMessageInput,
    GenerateOutreachInput,
    GenerateFollowUpInput,
    PlanNextActionInput,
)
from .base import AgentAdapter

logger = logging.getLogger("agent.openclaw")


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
class OpenClawTransport(ABC):
    name = "base"

    @abstractmethod
    def ping(self) -> bool: ...

    @abstractmethod
    def send_task(self, task: dict, timeout: int) -> dict: ...


class HTTPTransport(OpenClawTransport):
    name = "http"

    def __init__(self, endpoint: str, api_key: Optional[str], timeout: int):
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout

    def ping(self) -> bool:
        try:
            with httpx.Client(timeout=self.timeout) as c:
                r = c.get(self.endpoint.rstrip("/") + "/health")
                return r.status_code < 500
        except Exception:
            return False

    def send_task(self, task: dict, timeout: int) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        with httpx.Client(timeout=timeout) as c:
            r = c.post(self.endpoint.rstrip("/") + "/task", json=task, headers=headers)
            r.raise_for_status()
            return r.json()


class WebhookTransport(HTTPTransport):
    """Webhook is a fire-and-respond pattern; we POST and expect JSON back."""

    name = "webhook"


class CLITransport(OpenClawTransport):
    name = "cli"

    def __init__(self, command: str, timeout: int):
        self.command = command
        self.timeout = timeout

    def ping(self) -> bool:
        return bool(self.command)

    def send_task(self, task: dict, timeout: int) -> dict:
        payload = json.dumps(task)
        proc = subprocess.run(
            self.command, input=payload, capture_output=True, text=True,
            shell=True, timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"CLI exited {proc.returncode}: {proc.stderr[:300]}")
        return json.loads(proc.stdout)


class MCPTransport(OpenClawTransport):
    """MCP transport requires an MCP client/runner.

    We DO NOT fake results. If no MCP runtime is wired, send_task raises so the
    run is marked failed (never a random decision).
    """

    name = "mcp"

    def __init__(self, endpoint: Optional[str], timeout: int):
        self.endpoint = endpoint
        self.timeout = timeout

    def ping(self) -> bool:
        return False

    def send_task(self, task: dict, timeout: int) -> dict:
        raise NotImplementedError(
            "MCP transport requires an MCP client integration; not wired in this build. "
            "OpenClaw is reported as unreachable."
        )


def build_transport() -> Optional[OpenClawTransport]:
    s = get_settings()
    if not is_openclaw_configured(s):
        return None
    t = s.OPENCLAW_TRANSPORT
    if t in ("http",):
        return HTTPTransport(s.OPENCLAW_ENDPOINT, s.OPENCLAW_API_KEY, s.OPENCLAW_TIMEOUT_SECONDS)
    if t == "webhook":
        return WebhookTransport(s.OPENCLAW_ENDPOINT, s.OPENCLAW_API_KEY, s.OPENCLAW_TIMEOUT_SECONDS)
    if t == "cli":
        return CLITransport(s.OPENCLAW_COMMAND, s.OPENCLAW_TIMEOUT_SECONDS)
    if t == "mcp":
        return MCPTransport(s.OPENCLAW_ENDPOINT, s.OPENCLAW_TIMEOUT_SECONDS)
    return None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------
_SCHEMA_HINT = (
    "Respond ONLY with a JSON object matching this schema: "
    '{"intent": str, "confidence": float, "summary": str, '
    '"recommended_action": str, "reasoning_summary": str, "risk_level": str, '
    '"requires_approval": bool, "suggested_follow_up_at": str|null, '
    '"draft": {"subject": str, "body_text": str, "body_html": str}|null, '
    '"tool_requests": list, "model": str, "prompt_version": str}'
)

_VALID_ACTIONS = {"reply", "follow_up_later", "stop", "human_review", "no_action"}
_VALID_INTENTS = {"interested", "asking_question", "objection", "not_interested",
                  "unsubscribe", "opt_out", "out_of_office", "bounce", "unknown"}


class OpenClawAdapter(AgentAdapter):
    agent_name = "openclaw"

    def __init__(self, db=None):
        self._db = db
        self._transport = build_transport()

    def _task(self, method: str, inp: dict) -> dict:
        return {
            "method": method,
            "task_id": "oc_" + secrets.token_hex(10),
            "schema": _SCHEMA_HINT,
            "input": inp,
        }

    def _call(self, method: str, inp: dict) -> Optional[AgentDecision]:
        if self._transport is None:
            return None
        t0 = time.time()
        try:
            raw = self._transport.send_task(self._task(method, inp), get_settings().OPENCLAW_TIMEOUT_SECONDS)
        except Exception as e:
            logger.warning("OpenClaw %s failed: %s", method, e)
            # mark degraded (caller records). Return None => no fake result.
            return self._emit_failed(method, str(e)[:300], int((time.time() - t0) * 1000))
        decision = self._validate(raw)
        if decision is None:
            # ONE repair attempt
            try:
                repair = self._transport.send_task(
                    {**self._task(method, inp), "repair": True, "invalid_output": raw, "fix": _SCHEMA_HINT},
                    get_settings().OPENCLAW_TIMEOUT_SECONDS,
                )
                decision = self._validate(repair)
            except Exception as e:
                logger.warning("OpenClaw repair failed: %s", e)
        if decision is None:
            return self._emit_failed(method, "invalid output after repair", int((time.time() - t0) * 1000))
        decision.agent = "openclaw"
        decision.run_id = "run_" + secrets.token_hex(12)
        decision.latency_ms = int((time.time() - t0) * 1000)
        if not decision.model:
            decision.model = "openclaw"
        return decision

    def _emit_failed(self, method, error, latency) -> None:
        # Record a failed run so the UI/activity shows the failure (no fake decision).
        if self._db is not None:
            self._db.add(models.AgentRun(
                run_id="run_" + secrets.token_hex(12),
                agent="openclaw", task_type=method,
                status="failed", error=error, latency_ms=latency,
            ))
            self._db.flush()
        return None

    def _validate(self, raw: dict) -> Optional[AgentDecision]:
        try:
            d = AgentDecision(**raw)
        except Exception:
            # coerce common mistakes
            try:
                if isinstance(raw, dict):
                    raw.setdefault("agent", "openclaw")
                    raw.setdefault("run_id", "run_" + secrets.token_hex(12))
                    raw.setdefault("reasoning_summary", raw.get("summary", ""))
                    raw.setdefault("risk_level", "low")
                    raw.setdefault("requires_approval", True)
                    raw.setdefault("model", "openclaw")
                    raw.setdefault("prompt_version", "1.0")
                    d = AgentDecision(**raw)
                else:
                    return None
            except Exception:
                return None
        # enforce enum domains
        if d.intent not in _VALID_INTENTS:
            return None
        if d.recommended_action not in _VALID_ACTIONS:
            return None
        if d.risk_level not in ("low", "medium", "high"):
            return None
        return d

    def analyze_message(self, inp: AnalyzeMessageInput) -> Optional[AgentDecision]:
        return self._call("analyze_message", inp.model_dump())

    def generate_outreach(self, inp: GenerateOutreachInput) -> Optional[EmailProposal]:
        d = self._call("generate_outreach", inp.model_dump())
        return self._to_proposal(d) if d else None

    def generate_follow_up(self, inp: GenerateFollowUpInput) -> Optional[EmailProposal]:
        d = self._call("generate_follow_up", inp.model_dump())
        return self._to_proposal(d) if d else None

    def plan_next_action(self, inp: PlanNextActionInput) -> Optional[AgentDecision]:
        return self._call("plan_next_action", inp.model_dump())

    def health_check(self) -> AgentHealth:
        s = get_settings()
        cfg = is_openclaw_configured(s)
        if not cfg:
            return AgentHealth(agent="openclaw", configured=False, reachable=False,
                               detail="not configured (set OPENCLAW_ENDPOINT or OPENCLAW_COMMAND)")
        reachable = False
        detail = ""
        t0 = time.time()
        try:
            reachable = bool(self._transport and self._transport.ping())
            detail = "reachable" if reachable else "configured but unreachable"
        except Exception as e:
            detail = f"error: {str(e)[:120]}"
        return AgentHealth(agent="openclaw", configured=True, reachable=reachable,
                           mode=s.OPENCLAW_TRANSPORT, detail=detail,
                           latency_ms=int((time.time() - t0) * 1000))

    def _to_proposal(self, d: AgentDecision) -> EmailProposal:
        return EmailProposal(
            agent=d.agent, run_id=d.run_id,
            subject=d.draft.subject if d.draft else "",
            body_text=d.draft.body_text if d.draft else "",
            body_html=d.draft.body_html if d.draft else "",
            intent=d.intent, confidence=d.confidence, summary=d.summary,
            recommended_action=d.recommended_action, reasoning_summary=d.reasoning_summary,
            risk_level=d.risk_level, requires_approval=d.requires_approval,
            latency_ms=d.latency_ms, model=d.model, prompt_version=d.prompt_version,
        )
