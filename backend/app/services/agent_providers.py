"""Agent Provider boundary for interactive and scheduled agent sessions.

Providers orchestrate an Agent runtime only.  Email data and side effects stay
behind the typed ``ea_*`` MCP server and the existing backend policy engine.
"""
from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ..config import get_settings
from . import flags

SELECTED_PROVIDER_FLAG = "agent_provider_selected"
SESSION_PROVIDER_FLAG = "agent_takeover_session_provider"
DEFAULT_PROVIDER_ID = "tacwork"

CAP_INTERACTIVE_SESSION = "interactive_session"
CAP_SCHEDULED_TAKEOVER = "scheduled_takeover"
CAP_STREAMING = "streaming"
CAP_EMBEDDED_SURFACE = "embedded_surface"
CAP_MCP_TOOLS = "mcp_tools"

SESSION_STATUSES = {"created", "running", "completed", "failed", "stopped", "unknown"}


@dataclass
class AgentProviderError(RuntimeError):
    code: str
    detail: str = ""

    def __str__(self) -> str:
        return self.code if not self.detail else f"{self.code}:{self.detail}"


class AgentProvider(Protocol):
    id: str
    display_name: str

    @property
    def capabilities(self) -> tuple[str, ...]: ...
    @property
    def configured(self) -> bool: ...
    def health(self) -> dict: ...
    def create_session(self, *, title: str, prompt: str, system: str | None = None) -> dict: ...
    def get_session(self, *, workspace_id: str, session_id: str) -> dict: ...
    def send_message(self, *, workspace_id: str, session_id: str, message: str) -> dict: ...
    def stop_session(self, *, workspace_id: str, session_id: str) -> dict: ...
    def get_surface(self) -> dict: ...
    def has_active_session(self) -> bool: ...


class _HttpProvider:
    id = ""
    display_name = ""

    def _request(
        self,
        method: str,
        url: str,
        *,
        payload: dict | None = None,
        token: str | None = None,
        timeout: int,
    ) -> dict:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, method=method, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
                if not raw:
                    return {}
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict):
                    raise AgentProviderError("agent_provider_protocol_error", "response_not_object")
                return value
        except AgentProviderError:
            raise
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise AgentProviderError("agent_provider_protocol_error", f"http_{exc.code}:{detail}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise AgentProviderError("agent_provider_timeout", self.id) from exc
        except URLError as exc:
            if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
                raise AgentProviderError("agent_provider_timeout", self.id) from exc
            raise AgentProviderError("agent_provider_unreachable", self.id) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AgentProviderError("agent_provider_protocol_error", "invalid_json") from exc

    @staticmethod
    def _normalize_status(value: object) -> str:
        status = str(value or "unknown").strip().lower()
        aliases = {
            "idle": "completed",
            "complete": "completed",
            "done": "completed",
            "busy": "running",
            "pending": "created",
            "cancelled": "stopped",
            "canceled": "stopped",
            "error": "failed",
        }
        status = aliases.get(status, status)
        return status if status in SESSION_STATUSES else "unknown"


class TacWorkProvider(_HttpProvider):
    id = "tacwork"
    display_name = "TACWork"

    @property
    def settings(self):
        return get_settings()

    @property
    def configured(self) -> bool:
        return bool(self.settings.TACWORK_SERVER_URL and self.settings.TACWORK_CLIENT_TOKEN)

    @property
    def capabilities(self) -> tuple[str, ...]:
        if not self.configured:
            return ()
        return (
            CAP_INTERACTIVE_SESSION,
            CAP_SCHEDULED_TAKEOVER,
            CAP_STREAMING,
            CAP_EMBEDDED_SURFACE,
            CAP_MCP_TOOLS,
        )

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        if not self.configured:
            raise AgentProviderError("agent_provider_not_configured", self.id)
        return self._request(
            method,
            f"{self.settings.TACWORK_SERVER_URL.rstrip('/')}{path}",
            payload=payload,
            token=self.settings.TACWORK_CLIENT_TOKEN,
            timeout=self.settings.TACWORK_HTTP_TIMEOUT_SECONDS,
        )

    def health(self) -> dict:
        status = self._call("GET", "/status")
        workspace_id = str(status.get("activeWorkspaceId") or "").strip()
        if not workspace_id:
            raise AgentProviderError("agent_provider_protocol_error", "tacwork_has_no_active_workspace")
        return {"healthy": True, "workspace_id": workspace_id}

    def create_session(self, *, title: str, prompt: str, system: str | None = None) -> dict:
        workspace_id = self.health()["workspace_id"]
        payload = {"title": title, "prompt": prompt}
        if system:
            payload["system"] = system
        created = self._call("POST", f"/workspace/{quote(workspace_id)}/sessions", payload)
        session_id = str((created.get("item") or {}).get("id") or "").strip()
        if not session_id:
            raise AgentProviderError("agent_provider_protocol_error", "session_id_missing")
        return {"id": session_id, "workspace_id": workspace_id, "status": "running" if prompt else "created"}

    def get_session(self, *, workspace_id: str, session_id: str) -> dict:
        snapshot = self._call(
            "GET",
            f"/workspace/{quote(workspace_id)}/sessions/{quote(session_id)}/snapshot?limit=20",
        )
        item = snapshot.get("item") if isinstance(snapshot.get("item"), dict) else snapshot
        raw_status = item.get("status") if isinstance(item, dict) else None
        if isinstance(raw_status, dict):
            raw_status = raw_status.get("type")
        return {
            "id": session_id,
            "workspace_id": workspace_id,
            "status": self._normalize_status(raw_status),
            "snapshot": item,
        }

    def send_message(self, *, workspace_id: str, session_id: str, message: str) -> dict:
        result = self._call(
            "POST",
            f"/workspace/{quote(workspace_id)}/opencode/session/{quote(session_id)}/prompt_async",
            {"parts": [{"type": "text", "text": message}]},
        )
        return {"id": session_id, "workspace_id": workspace_id, "status": "running", "result": result}

    def stop_session(self, *, workspace_id: str, session_id: str) -> dict:
        self._call(
            "POST",
            f"/workspace/{quote(workspace_id)}/opencode/session/{quote(session_id)}/abort",
            {},
        )
        return {"id": session_id, "workspace_id": workspace_id, "status": "stopped"}

    def get_surface(self) -> dict:
        health = self.health()
        workspace_id = health["workspace_id"]
        workspace_url = f"{self.settings.TACWORK_WEB_URL.rstrip('/')}/workspace/{quote(workspace_id)}/session"
        latest_url = workspace_url
        sessions = self._call("GET", f"/workspace/{quote(workspace_id)}/sessions?roots=true&limit=200")
        items = sessions.get("items") if isinstance(sessions.get("items"), list) else []
        roots = [item for item in items if isinstance(item, dict) and item.get("id") and not item.get("parentID") and not (item.get("time") or {}).get("archived")]
        if roots:
            roots.sort(key=lambda item: (item.get("time") or {}).get("updated") or (item.get("time") or {}).get("created") or 0, reverse=True)
            latest_url = f"{workspace_url}/{quote(str(roots[0]['id']))}"
        return {
            "type": "embedded_url",
            "url": latest_url,
            "new_session_url": workspace_url,
            "external_url": latest_url,
        }

    def has_active_session(self) -> bool:
        workspace_id = self.health()["workspace_id"]
        try:
            payload = self._call("GET", f"/workspace/{quote(workspace_id)}/opencode/session/status")
        except AgentProviderError as exc:
            if exc.code == "agent_provider_protocol_error" and "http_404" in exc.detail:
                return False
            raise
        statuses = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(statuses, dict):
            raise AgentProviderError("agent_provider_protocol_error", "session_status_not_object")
        return any(
            isinstance(value, dict) and str(value.get("type") or "idle").lower() not in {"idle", "completed", "stopped"}
            for value in statuses.values()
        )


class DeepSeekHarnessProvider(_HttpProvider):
    id = "deepseek_harness"
    display_name = "DeepSeek Harness"

    @property
    def settings(self):
        return get_settings()

    @property
    def configured(self) -> bool:
        return bool(self.settings.DEEPSEEK_HARNESS_URL and self.settings.DEEPSEEK_HARNESS_MCP_ENABLED)

    @property
    def capabilities(self) -> tuple[str, ...]:
        if not self.configured:
            return ()
        values = [CAP_INTERACTIVE_SESSION, CAP_STREAMING, CAP_MCP_TOOLS]
        if self.settings.DEEPSEEK_HARNESS_SCHEDULED_TAKEOVER:
            values.append(CAP_SCHEDULED_TAKEOVER)
        if self.settings.DEEPSEEK_HARNESS_SURFACE_TYPE in {"embedded_url", "event_stream"}:
            values.append(CAP_EMBEDDED_SURFACE)
        return tuple(values)

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        if not self.configured:
            raise AgentProviderError("agent_provider_not_configured", self.id)
        return self._request(
            method,
            f"{self.settings.DEEPSEEK_HARNESS_URL.rstrip('/')}{path}",
            payload=payload,
            token=self.settings.DEEPSEEK_HARNESS_API_KEY,
            timeout=self.settings.DEEPSEEK_HARNESS_TIMEOUT_SECONDS,
        )

    def health(self) -> dict:
        result = self._call("GET", "/health")
        healthy = result.get("ok") is True or str(result.get("status") or "").lower() in {"ok", "healthy"}
        if not healthy:
            raise AgentProviderError("agent_provider_unreachable", self.id)
        capabilities = result.get("capabilities") if isinstance(result.get("capabilities"), list) else []
        if result.get("mcp_connected") is not True and CAP_MCP_TOOLS not in capabilities:
            raise AgentProviderError("agent_provider_capability_missing", f"{self.id}:{CAP_MCP_TOOLS}")
        return {"healthy": True, "workspace_id": str(result.get("workspace_id") or "default")}

    def create_session(self, *, title: str, prompt: str, system: str | None = None) -> dict:
        payload = {"title": title, "prompt": prompt}
        if system:
            payload["system"] = system
        result = self._call("POST", "/sessions", payload)
        item = result.get("session") if isinstance(result.get("session"), dict) else result
        session_id = str(item.get("id") or "").strip()
        if not session_id:
            raise AgentProviderError("agent_provider_protocol_error", "session_id_missing")
        return {
            "id": session_id,
            "workspace_id": str(item.get("workspace_id") or "default"),
            "status": self._normalize_status(item.get("status") or "running"),
        }

    def get_session(self, *, workspace_id: str, session_id: str) -> dict:
        result = self._call("GET", f"/sessions/{quote(session_id)}")
        item = result.get("session") if isinstance(result.get("session"), dict) else result
        return {
            "id": session_id,
            "workspace_id": str(item.get("workspace_id") or workspace_id or "default"),
            "status": self._normalize_status(item.get("status")),
            "snapshot": item,
        }

    def send_message(self, *, workspace_id: str, session_id: str, message: str) -> dict:
        result = self._call("POST", f"/sessions/{quote(session_id)}/messages", {"message": message})
        return {"id": session_id, "workspace_id": workspace_id or "default", "status": "running", "result": result}

    def stop_session(self, *, workspace_id: str, session_id: str) -> dict:
        self._call("POST", f"/sessions/{quote(session_id)}/stop", {})
        return {"id": session_id, "workspace_id": workspace_id or "default", "status": "stopped"}

    def get_surface(self) -> dict:
        surface_type = self.settings.DEEPSEEK_HARNESS_SURFACE_TYPE.strip().lower()
        url = self.settings.DEEPSEEK_HARNESS_SURFACE_URL
        if surface_type not in {"embedded_url", "event_stream", "external_url"} or not url:
            return {"type": "unavailable", "reason": "agent_provider_surface_unavailable"}
        return {"type": surface_type, "url": url, "external_url": url}

    def has_active_session(self) -> bool:
        result = self._call("GET", "/sessions?status=running")
        sessions = result.get("sessions") if isinstance(result.get("sessions"), list) else result.get("items")
        return bool(sessions)


def provider_registry() -> dict[str, AgentProvider]:
    return {
        TacWorkProvider.id: TacWorkProvider(),
        DeepSeekHarnessProvider.id: DeepSeekHarnessProvider(),
    }


def selected_provider_id(db) -> str:
    value = (flags.get_flag(db, SELECTED_PROVIDER_FLAG, DEFAULT_PROVIDER_ID) or DEFAULT_PROVIDER_ID).strip().lower()
    return value if value in provider_registry() else DEFAULT_PROVIDER_ID


def get_provider(provider_id: str) -> AgentProvider:
    provider = provider_registry().get(provider_id)
    if provider is None:
        raise AgentProviderError("agent_provider_not_configured", provider_id)
    return provider


def get_selected_provider(db) -> AgentProvider:
    return get_provider(selected_provider_id(db))


def require_capability(provider: AgentProvider, capability: str) -> None:
    if not provider.configured:
        raise AgentProviderError("agent_provider_not_configured", provider.id)
    if capability not in provider.capabilities:
        raise AgentProviderError("agent_provider_capability_missing", f"{provider.id}:{capability}")


def public_provider(provider: AgentProvider, *, include_surface: bool = False) -> dict:
    result = {
        "id": provider.id,
        "display_name": provider.display_name,
        "configured": provider.configured,
        "capabilities": list(provider.capabilities),
    }
    if include_surface:
        if not provider.configured:
            result["surface"] = {"type": "unavailable", "reason": "agent_provider_not_configured"}
        else:
            try:
                result["surface"] = provider.get_surface()
            except AgentProviderError as exc:
                result["surface"] = {"type": "unavailable", "reason": exc.code}
    return result
