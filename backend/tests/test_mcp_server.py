import importlib.util
from pathlib import Path


PATH = Path(__file__).resolve().parents[2] / "scripts" / "mcp_server.py"
SPEC = importlib.util.spec_from_file_location("email_automation_mcp", PATH)
MCP = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MCP)


def test_protocol_lists_expected_tools():
    reply = MCP.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {item["name"] for item in reply["result"]["tools"]}
    assert {"ea_takeover_status", "ea_health", "ea_dashboard", "ea_start_agent_run", "ea_confirm_run", "ea_agent_report"} <= names


def test_takeover_status_is_safe_read_only_aggregate(monkeypatch):
    calls = []
    replies = {
        "/api/health": {"status": "ok", "consumer": {"healthy": True}},
        "/api/gmail/status": {"connected": True, "email": "operator@example.com"},
        "/api/system/pause": {"global_pause": False},
        "/api/agent/health": {"langgraph": {"configured": True, "reachable": True}},
        "/api/agent-profile?create_if_missing=false": {"configured": True, "approval_mode": "human_review"},
        "/api/dashboard/readiness": {"status": "ready", "next_action": "review_needs_reply", "real_send": True, "blockers": [], "awaiting_confirmation_runs": []},
        "/api/dashboard/metrics": {"langgraph_configured": True},
        "/api/inbox/stats": {"human_review": 2, "needs_reply": 1},
        "/api/contacts": [{"id": 1}],
        "/api/campaigns": [], "/api/automation": [], "/api/approvals?status=pending": [],
        "/api/inbox/threads?category=unsorted&limit=500": [],
        "/api/automation/scheduler/status": {"status": "running"},
        "/api/agent-takeover": {"enabled": False, "current_stage": "disabled"},
    }
    monkeypatch.setattr(MCP, "_request", lambda method, path, *a, **k: calls.append((method, path)) or replies[path])

    reply = MCP.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "ea_takeover_status", "arguments": {}}})
    value = reply["result"]["structuredContent"]
    assert reply["result"].get("isError") is not True
    assert all(method == "GET" for method, _ in calls)
    assert value["workspace_mode"] == "production"
    assert value["profile"]["approval_mode"] == "human_review"
    assert value["agent_takeover"]["enabled"] is False
    assert value["counts"] == {"contacts": 1, "campaigns": 0, "automations": 0, "pending_approvals": 0, "unsorted_threads": 0, "human_review_threads": 2, "needs_reply_threads": 1}
    assert "api_key" not in str(value).lower()
    assert "token" not in str(value).lower()


def test_write_tools_require_explicit_authorization(monkeypatch):
    monkeypatch.setattr(MCP, "_request", lambda *args, **kwargs: {"unexpected": True})
    reply = MCP.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "ea_sync_gmail", "arguments": {"user_authorized": False}}})
    assert reply["result"]["isError"] is True
    assert "user_authorized=true" in reply["result"]["content"][0]["text"]


def test_read_tool_delegates_to_loopback_api(monkeypatch):
    calls = []
    monkeypatch.setattr(MCP, "_request", lambda method, path, *args, **kwargs: calls.append((method, path)) or {"status": "ok"})
    reply = MCP.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ea_health", "arguments": {}}})
    assert calls == [("GET", "/api/health")]
    assert reply["result"]["structuredContent"]["status"] == "ok"


def test_scheduled_takeover_revalidates_capability_before_global_run(monkeypatch):
    calls = []

    def request(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if path == "/api/agent-takeover/authorize":
            return {"authorized": True}
        if path == "/api/agent-takeover/telemetry":
            return {"ok": True}
        if path == "/api/agent-runs":
            return {"run_id": 9, "status": "queued", "mode": "full_auto"}
        raise AssertionError(path)

    monkeypatch.setattr(MCP, "_request", request)
    reply = MCP.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {
        "name": "ea_start_agent_run", "arguments": {
            "automation_id": 7, "mode": "full_auto", "user_authorized": True,
            "authorization_source": "agent_takeover", "takeover_token": "capability",
        },
    }})
    assert reply["result"].get("isError") is not True
    assert calls[0] == ("POST", "/api/agent-takeover/authorize", {
        "token": "capability", "operation": "start_global_run", "automation_id": 7,
    })
    assert calls[1][1:] == ("/api/agent-takeover/telemetry", {
        "token": "capability", "stage": "start_global_run", "status": "started", "detail": {},
    })
    assert calls[2][1] == "/api/agent-runs"
    assert calls[3][1] == "/api/agent-takeover/telemetry"


def test_telemetry_failure_does_not_block_authorized_operation(monkeypatch):
    calls = []

    def request(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if path == "/api/agent-takeover/authorize":
            return {"authorized": True}
        if path == "/api/agent-takeover/telemetry":
            raise RuntimeError("telemetry unavailable")
        if path == "/api/gmail/sync":
            return {"threads": 1}
        raise AssertionError(path)

    monkeypatch.setattr(MCP, "_request", request)
    reply = MCP.handle({"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {
        "name": "ea_sync_gmail", "arguments": {
            "user_authorized": True, "authorization_source": "agent_takeover",
            "takeover_token": "capability",
        },
    }})
    assert reply["result"].get("isError") is not True
    assert any(path == "/api/gmail/sync" for _, path, _ in calls)


def test_new_knowledge_and_profile_tools_registered():
    reply = MCP.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
    names = {item["name"] for item in reply["result"]["tools"]}
    expected = {
        "ea_list_knowledge", "ea_get_knowledge", "ea_search_knowledge",
        "ea_update_knowledge", "ea_publish_knowledge", "ea_disable_knowledge",
        "ea_delete_knowledge", "ea_get_profile", "ea_configure_profile", "ea_set_approval_mode",
    }
    assert expected <= names


def test_delete_knowledge_requires_authorization(monkeypatch):
    monkeypatch.setattr(MCP, "_request", lambda *a, **k: {"unexpected": True})
    reply = MCP.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
        "name": "ea_delete_knowledge", "arguments": {"document_id": 1, "user_authorized": False}}})
    assert reply["result"]["isError"] is True
    assert "user_authorized=true" in reply["result"]["content"][0]["text"]


def test_dispatch_routes_knowledge_and_profile_tools(monkeypatch):
    calls = []
    monkeypatch.setattr(MCP, "_request", lambda method, path, *a, **k: calls.append((method, path)) or {"ok": True})

    def call(name, arguments):
        return MCP.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}})

    call("ea_list_knowledge", {})
    call("ea_get_knowledge", {"document_id": 7})
    call("ea_search_knowledge", {"query": "pricing"})
    call("ea_delete_knowledge", {"document_id": 7, "user_authorized": True})
    call("ea_update_knowledge", {"document_id": 7, "title": "New Title", "content": "fresh", "user_authorized": True})
    call("ea_publish_knowledge", {"document_id": 7, "user_authorized": True})
    call("ea_disable_knowledge", {"document_id": 7, "user_authorized": True})
    call("ea_configure_profile", {"agent_name": "Sendy", "company_name": "TAC AISolution",
                                  "role": "Sales Consultant", "tone": "professional",
                                  "language_policy": "match_customer", "signature_text": "Best regards,\nSendy",
                                  "unknown_answer_policy": "Confirm and propose next step.",
                                  "user_authorized": True})
    call("ea_get_profile", {})
    call("ea_set_approval_mode", {"approval_mode": "human_review", "user_authorized": True})

    assert calls[0] == ("GET", "/api/knowledge")
    assert calls[1] == ("GET", "/api/knowledge/7")
    assert calls[2] == ("POST", "/api/knowledge/search")
    assert calls[3] == ("DELETE", "/api/knowledge/7")
    assert calls[4] == ("PUT", "/api/knowledge/7")
    assert calls[5] == ("POST", "/api/knowledge/7/publish")
    assert calls[6] == ("POST", "/api/knowledge/7/disable")
    assert calls[7] == ("PUT", "/api/agent-profile?actor=agent")
    assert calls[8] == ("GET", "/api/agent-profile?create_if_missing=false")
    assert calls[9] == ("PUT", "/api/agent-profile/approval-mode?actor=agent")
