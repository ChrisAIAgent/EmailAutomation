from __future__ import annotations

from types import SimpleNamespace
from urllib.error import URLError

from app import credential_store, models
from app.config import get_settings
from app.services import agent_providers as provider_svc
from app.services import agent_takeover as takeover_svc
from app.services import flags


def test_provider_catalog_defaults_to_tacwork_and_never_returns_tokens(client):
    response = client.get("/api/agent-providers")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["selected_provider"] == "tacwork"
    assert [item["id"] for item in payload["providers"]] == ["tacwork", "deepseek_harness"]
    assert payload["providers"][0]["configured"] is True
    assert payload["providers"][1]["configured"] is False
    assert "email-automation-local-v1" not in response.text
    assert "api_key" not in response.text.lower()


def test_unconfigured_provider_cannot_replace_current_selection(client, db):
    response = client.put("/api/agent-provider", json={"provider_id": "deepseek_harness"})
    assert response.status_code == 409
    assert response.json()["detail"] == "agent_provider_not_configured"
    assert provider_svc.selected_provider_id(db) == "tacwork"
    assert db.query(models.AuditLog).filter_by(action="agent_provider_changed").count() == 0


def test_provider_switch_is_audited_and_does_not_touch_business_rows(client, db, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_HARNESS_URL", "http://127.0.0.1:29002")
    monkeypatch.setenv("DEEPSEEK_HARNESS_MCP_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(provider_svc.DeepSeekHarnessProvider, "health", lambda self: {"healthy": True, "workspace_id": "default"})
    monkeypatch.setattr(provider_svc.TacWorkProvider, "has_active_session", lambda self: False)

    response = client.put("/api/agent-provider", json={"provider_id": "deepseek_harness"})
    assert response.status_code == 200, response.text
    assert response.json()["selected_provider"] == "deepseek_harness"
    assert flags.get_flag(db, provider_svc.SELECTED_PROVIDER_FLAG) == "deepseek_harness"
    row = db.query(models.AuditLog).filter_by(action="agent_provider_changed").one()
    assert '"from": "tacwork"' in row.detail
    assert '"to": "deepseek_harness"' in row.detail
    assert db.query(models.Approval).count() == 0
    assert db.query(models.EmailDraft).count() == 0


def test_running_session_blocks_provider_switch(client, db, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_HARNESS_URL", "http://127.0.0.1:29002")
    monkeypatch.setenv("DEEPSEEK_HARNESS_MCP_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(provider_svc.DeepSeekHarnessProvider, "health", lambda self: {"healthy": True, "workspace_id": "default"})
    flags.set_flag(db, takeover_svc.FLAG_ACTIVE_SESSION, "ses_running")
    db.commit()

    response = client.put("/api/agent-provider", json={"provider_id": "deepseek_harness"})
    assert response.status_code == 409
    assert response.json()["detail"] == "agent_provider_session_active"
    assert provider_svc.selected_provider_id(db) == "tacwork"


def test_takeover_requires_selected_provider_scheduled_capability(client, db, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_HARNESS_URL", "http://127.0.0.1:29002")
    monkeypatch.setenv("DEEPSEEK_HARNESS_MCP_ENABLED", "true")
    monkeypatch.setenv("DEEPSEEK_HARNESS_SCHEDULED_TAKEOVER", "false")
    get_settings.cache_clear()
    flags.set_flag(db, provider_svc.SELECTED_PROVIDER_FLAG, "deepseek_harness")
    db.commit()

    response = client.post("/api/agent-takeover", json={"enabled": True, "interval_minutes": 15})
    assert response.status_code == 409
    assert response.json()["detail"] == "agent_provider_capability_missing"
    assert takeover_svc.status(db)["enabled"] is False


def test_tacwork_provider_maps_session_protocol(monkeypatch):
    provider = provider_svc.TacWorkProvider()
    calls = []

    def fake_call(method, path, payload=None):
        calls.append((method, path, payload))
        if path == "/status":
            return {"activeWorkspaceId": "ws_email"}
        if path == "/workspace/ws_email/sessions":
            return {"item": {"id": "ses_1"}}
        if path.endswith("/snapshot?limit=20"):
            return {"item": {"status": {"type": "idle"}}}
        return {}

    monkeypatch.setattr(provider, "_call", fake_call)
    created = provider.create_session(title="Title", prompt="Run", system="sealed")
    assert created == {"id": "ses_1", "workspace_id": "ws_email", "status": "running"}
    assert provider.get_session(workspace_id="ws_email", session_id="ses_1")["status"] == "completed"
    provider.send_message(workspace_id="ws_email", session_id="ses_1", message="Continue")
    provider.stop_session(workspace_id="ws_email", session_id="ses_1")
    assert any("prompt_async" in path for _method, path, _payload in calls)
    assert any(path.endswith("/abort") for _method, path, _payload in calls)


def test_deepseek_harness_provider_contract_uses_sessions_and_requires_mcp(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_HARNESS_URL", "http://127.0.0.1:29002")
    monkeypatch.setenv("DEEPSEEK_HARNESS_MCP_ENABLED", "true")
    monkeypatch.setenv("DEEPSEEK_HARNESS_SCHEDULED_TAKEOVER", "true")
    monkeypatch.setenv("DEEPSEEK_HARNESS_SURFACE_TYPE", "event_stream")
    monkeypatch.setenv("DEEPSEEK_HARNESS_SURFACE_URL", "http://127.0.0.1:29002/events")
    get_settings.cache_clear()
    provider = provider_svc.DeepSeekHarnessProvider()
    calls = []

    def fake_call(method, path, payload=None):
        calls.append((method, path, payload))
        if path == "/health":
            return {"status": "ok", "mcp_connected": True, "workspace_id": "email"}
        if path == "/sessions":
            return {"session": {"id": "deep_1", "workspace_id": "email", "status": "running"}}
        if path == "/sessions/deep_1":
            return {"session": {"id": "deep_1", "status": "completed"}}
        if path == "/sessions?status=running":
            return {"sessions": []}
        return {"ok": True}

    monkeypatch.setattr(provider, "_call", fake_call)
    assert provider.health()["workspace_id"] == "email"
    assert provider_svc.CAP_SCHEDULED_TAKEOVER in provider.capabilities
    assert provider.create_session(title="Title", prompt="Prompt", system="sealed")["id"] == "deep_1"
    assert provider.get_session(workspace_id="email", session_id="deep_1")["status"] == "completed"
    assert provider.send_message(workspace_id="email", session_id="deep_1", message="Continue")["status"] == "running"
    assert provider.stop_session(workspace_id="email", session_id="deep_1")["status"] == "stopped"
    assert provider.has_active_session() is False
    assert provider.get_surface()["type"] == "event_stream"
    assert all("/api/" not in path for _method, path, _payload in calls)


def test_deepseek_health_rejects_plain_model_endpoint_without_mcp(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_HARNESS_URL", "http://127.0.0.1:29002")
    monkeypatch.setenv("DEEPSEEK_HARNESS_MCP_ENABLED", "true")
    get_settings.cache_clear()
    provider = provider_svc.DeepSeekHarnessProvider()
    monkeypatch.setattr(provider, "_call", lambda *_args, **_kwargs: {"status": "ok"})
    try:
        provider.health()
        raise AssertionError("plain model endpoint was accepted")
    except provider_svc.AgentProviderError as exc:
        assert exc.code == "agent_provider_capability_missing"


def test_provider_http_failures_use_stable_error_codes(monkeypatch):
    provider = provider_svc.TacWorkProvider()
    monkeypatch.setattr(provider_svc, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()))
    try:
        provider._call("GET", "/status")
        raise AssertionError("timeout not raised")
    except provider_svc.AgentProviderError as exc:
        assert exc.code == "agent_provider_timeout"

    monkeypatch.setattr(provider_svc, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")))
    try:
        provider._call("GET", "/status")
        raise AssertionError("unreachable not raised")
    except provider_svc.AgentProviderError as exc:
        assert exc.code == "agent_provider_unreachable"


def test_macos_keychain_round_trip_uses_security_without_credentials_file(tmp_path, monkeypatch):
    saved = {}

    def fake_run(args, **_kwargs):
        if args[1] == "add-generic-password":
            saved["value"] = args[args.index("-w") + 1]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "value" not in saved:
            return SimpleNamespace(returncode=44, stdout="", stderr="not found")
        return SimpleNamespace(returncode=0, stdout=saved["value"] + "\n", stderr="")

    monkeypatch.setattr(credential_store.sys, "platform", "darwin")
    monkeypatch.setattr(credential_store.subprocess, "run", fake_run)
    assert credential_store.load(tmp_path) == {}
    credential_store.save(tmp_path, {"oauth": {"client_id": "private"}})
    assert credential_store.load(tmp_path) == {"oauth": {"client_id": "private"}}
    assert not (tmp_path / "credentials.dat").exists()
