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
    assert {
        "ea_takeover_status", "ea_health", "ea_dashboard", "ea_start_agent_run",
        "ea_confirm_run", "ea_agent_report", "ea_get_campaign",
        "ea_list_campaign_members", "ea_add_campaign_contacts",
        "ea_remove_campaign_contact", "ea_generate_campaign_outreach",
        "ea_get_campaign_generation",
        "ea_generate_inbox_reply", "ea_revise_approval", "ea_create_automation",
    } <= names


def test_takeover_status_is_safe_read_only_aggregate(monkeypatch):
    calls = []
    replies = {
        "/api/health": {"status": "ok", "consumer": {"healthy": True}},
        "/api/gmail/status": {"connected": True, "email": "operator@example.com"},
        "/api/system/pause": {"global_pause": False},
        "/api/agent/health": [{"agent": "langgraph", "configured": True, "reachable": True, "detail": "LLM-backed"}],
        "/api/agent-profile?create_if_missing=false": {"configured": True, "approval_mode": "human_review"},
        "/api/dashboard/readiness": {"status": "ready", "next_action": "review_needs_reply", "real_send": True, "blockers": [], "awaiting_confirmation_runs": []},
        "/api/dashboard/metrics": {"langgraph_configured": True},
        "/api/inbox/stats": {"human_review": 2, "needs_reply": 1, "unprocessed": 0},
        "/api/contacts": [{"id": 1}],
        "/api/campaigns": [], "/api/automation": [], "/api/approvals?status=pending": [],
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
    assert value["ai"]["langgraph_reachable"] is True
    assert value["ai"]["provider_reachable"] is None
    assert value["counts"] == {"contacts": 1, "campaigns": 0, "automations": 0, "pending_approvals": 0, "unsorted_threads": 0, "untriaged_threads": 0, "human_review_threads": 2, "needs_reply_threads": 1}
    assert "/api/inbox/threads?category=unsorted&limit=500" not in {path for _, path in calls}
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


def test_mcp_timeout_is_structured_and_never_requests_automatic_retry(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise MCP.ApiRequestError("timed out", boundary="mcp_http_timeout", request_id="req-1")

    monkeypatch.setattr(MCP, "_request", timeout)
    reply = MCP.handle({"jsonrpc": "2.0", "id": 31, "method": "tools/call", "params": {"name": "ea_health", "arguments": {}}})
    error = reply["result"]["structuredContent"]
    assert reply["result"]["isError"] is True
    assert error["boundary"] == "mcp_http_timeout"
    assert error["request_id"] == "req-1"
    assert error["result_unknown"] is True
    assert error["retry_automatically"] is False


def test_scheduled_read_failure_is_reported_to_takeover_telemetry(monkeypatch):
    calls = []

    def request(method, path, body=None, **_kwargs):
        calls.append((method, path, body))
        if path == "/api/inbox/daily-triage/current":
            raise MCP.ApiRequestError("timed out", boundary="mcp_http_timeout", request_id="req-2")
        if path == "/api/agent-takeover/telemetry":
            return {"ok": True}
        raise AssertionError(path)

    monkeypatch.setattr(MCP, "_request", request)
    reply = MCP.handle({"jsonrpc": "2.0", "id": 32, "method": "tools/call", "params": {
        "name": "ea_daily_triage_status", "arguments": {
            "authorization_source": "agent_takeover", "takeover_token": "capability",
        },
    }})
    assert reply["result"]["isError"] is True
    assert calls[-1] == ("POST", "/api/agent-takeover/telemetry", {
        "token": "capability", "stage": "read_daily_triage", "status": "failed",
        "detail": {"error": "timed out"},
    })


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
        "token": "capability", "operation": "start_agent_run", "automation_id": 7,
    })
    assert calls[1][1:] == ("/api/agent-takeover/telemetry", {
        "token": "capability", "stage": "start_agent_run", "status": "started", "detail": {},
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


def test_campaign_and_automation_workflow_uses_typed_loopback_tools(monkeypatch):
    calls = []

    def request(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return {"ok": True}

    monkeypatch.setattr(MCP, "_request", request)

    def call(name, arguments):
        reply = MCP.handle({"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                            "params": {"name": name, "arguments": arguments}})
        assert reply["result"].get("isError") is not True

    authorized = {"user_authorized": True}
    call("ea_get_campaign", {"campaign_id": 5})
    call("ea_list_campaign_members", {"campaign_id": 5, "include_removed": True})
    call("ea_add_campaign_contacts", {"campaign_id": 5, "contact_ids": [3], **authorized})
    call("ea_remove_campaign_contact", {"campaign_id": 5, "contact_id": 3, "confirmed_removal": True, **authorized})
    call("ea_generate_campaign_outreach", {"campaign_id": 5, **authorized})
    call("ea_start_campaign", {"campaign_id": 5, **authorized})
    call("ea_generate_automation_plan", {"prompt": "Follow up once", "campaign_id": 5, **authorized})
    call("ea_create_automation", {"prompt": "Follow up once", "campaign_id": 5, "plan": {}, **authorized})
    call("ea_enable_automation", {"automation_id": 8, **authorized})
    call("ea_schedule_automation", {"automation_id": 8, "tick_interval_minutes": 60, **authorized})

    assert calls == [
        ("GET", "/api/campaigns/5", None),
        ("GET", "/api/campaigns/5/contacts?include_removed=true", None),
        ("POST", "/api/campaigns/5/contacts", {"contact_ids": [3]}),
        ("DELETE", "/api/campaigns/5/contacts/3", None),
        ("POST", "/api/campaigns/5/generate", None),
        ("POST", "/api/campaigns/5/start", None),
        ("POST", "/api/automation/generate", {"prompt": "Follow up once", "campaign_id": 5}),
        ("POST", "/api/automation", {"prompt": "Follow up once", "campaign_id": 5, "plan": {}}),
        ("POST", "/api/automation/8/enable", None),
        ("POST", "/api/automation/8/schedule", {"tick_interval_minutes": 60}),
    ]


def test_add_campaign_members_requires_explicit_authorization(monkeypatch):
    monkeypatch.setattr(MCP, "_request", lambda *args, **kwargs: {"unexpected": True})
    reply = MCP.handle({"jsonrpc": "2.0", "id": 13, "method": "tools/call", "params": {
        "name": "ea_add_campaign_contacts", "arguments": {
            "campaign_id": 5, "contact_ids": [3], "user_authorized": False,
        },
    }})
    assert reply["result"]["isError"] is True
    assert "user_authorized=true" in reply["result"]["content"][0]["text"]


def test_destructive_campaign_or_approval_actions_require_specific_confirmation(monkeypatch):
    calls = []
    monkeypatch.setattr(MCP, "_request", lambda *args, **kwargs: calls.append(args) or {"unexpected": True})

    remove = MCP.handle({"jsonrpc": "2.0", "id": 130, "method": "tools/call", "params": {
        "name": "ea_remove_campaign_contact", "arguments": {
            "campaign_id": 5, "contact_id": 3, "user_authorized": True,
        },
    }})
    invalidate = MCP.handle({"jsonrpc": "2.0", "id": 131, "method": "tools/call", "params": {
        "name": "ea_invalidate_approval", "arguments": {
            "approval_id": 7, "reason": "revision failed", "user_authorized": True,
        },
    }})

    assert remove["result"]["isError"] is True
    assert "confirmed_removal=true" in remove["result"]["content"][0]["text"]
    assert invalidate["result"]["isError"] is True
    assert "reason_category" in invalidate["result"]["content"][0]["text"]
    assert calls == []


def test_takeover_revalidates_target_campaign_before_member_change(monkeypatch):
    calls = []

    def request(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if path == "/api/agent-takeover/authorize":
            return {"authorized": True}
        if path == "/api/agent-takeover/telemetry":
            return {"ok": True}
        if path == "/api/campaigns/5/contacts":
            return {"added": 1}
        raise AssertionError(path)

    monkeypatch.setattr(MCP, "_request", request)
    reply = MCP.handle({"jsonrpc": "2.0", "id": 14, "method": "tools/call", "params": {
        "name": "ea_add_campaign_contacts", "arguments": {
            "campaign_id": 5, "contact_ids": [3], "user_authorized": True,
            "authorization_source": "agent_takeover", "takeover_token": "capability",
        },
    }})
    assert reply["result"].get("isError") is not True
    assert calls[0] == ("POST", "/api/agent-takeover/authorize", {
        "token": "capability", "operation": "add_campaign_contacts", "campaign_id": 5,
    })
    assert any(path == "/api/campaigns/5/contacts" for _, path, _ in calls)


def test_inbox_reply_tool_requires_authorization_and_routes_to_existing_thread(monkeypatch):
    calls = []

    def request(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return {"approval_id": 7, "message": "Reply draft generated."}

    monkeypatch.setattr(MCP, "_request", request)
    denied = MCP.handle({"jsonrpc": "2.0", "id": 15, "method": "tools/call", "params": {
        "name": "ea_generate_inbox_reply", "arguments": {"thread_id": 9, "user_authorized": False},
    }})
    assert denied["result"]["isError"] is True
    assert calls == []

    allowed = MCP.handle({"jsonrpc": "2.0", "id": 16, "method": "tools/call", "params": {
        "name": "ea_generate_inbox_reply", "arguments": {"thread_id": 9, "user_authorized": True},
    }})
    assert allowed["result"].get("isError") is not True
    assert calls == [("POST", "/api/inbox/threads/9/generate-reply", None)]


def test_revise_approval_tool_requires_authorization_and_never_sends(monkeypatch):
    calls = []

    def request(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return {"ok": True, "approval_id": 7, "status": "pending", "sent": False}

    monkeypatch.setattr(MCP, "_request", request)
    denied = MCP.handle({"jsonrpc": "2.0", "id": 18, "method": "tools/call", "params": {
        "name": "ea_revise_approval", "arguments": {
            "approval_id": 7, "instruction": "Make it shorter.", "user_authorized": False,
        },
    }})
    assert denied["result"]["isError"] is True
    assert calls == []

    allowed = MCP.handle({"jsonrpc": "2.0", "id": 19, "method": "tools/call", "params": {
        "name": "ea_revise_approval", "arguments": {
            "approval_id": 7, "instruction": "Make it shorter.", "user_authorized": True,
        },
    }})
    assert allowed["result"].get("isError") is not True
    assert calls == [("POST", "/api/approvals/7/revise", {"instruction": "Make it shorter."})]


def test_takeover_revalidates_owned_approval_before_revision(monkeypatch):
    calls = []

    def request(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if path == "/api/agent-takeover/authorize":
            return {"authorized": True}
        if path == "/api/agent-takeover/telemetry":
            return {"ok": True}
        if path == "/api/approvals/7/revise":
            return {"ok": True, "approval_id": 7, "draft_id": 4, "revision_applied": True, "sent": False}
        raise AssertionError(path)

    monkeypatch.setattr(MCP, "_request", request)
    reply = MCP.handle({"jsonrpc": "2.0", "id": 20, "method": "tools/call", "params": {
        "name": "ea_revise_approval", "arguments": {
            "approval_id": 7, "instruction": "Use a clearer CTA.", "user_authorized": True,
            "authorization_source": "agent_takeover", "takeover_token": "capability",
        },
    }})
    assert reply["result"].get("isError") is not True
    assert calls[0] == ("POST", "/api/agent-takeover/authorize", {
        "token": "capability", "operation": "revise_approval", "approval_id": 7,
    })
    assert any(path == "/api/approvals/7/revise" for _, path, _ in calls)


def test_takeover_revalidates_target_thread_before_inbox_reply(monkeypatch):
    calls = []

    def request(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if path == "/api/agent-takeover/authorize":
            return {"authorized": True}
        if path == "/api/agent-takeover/telemetry":
            return {"ok": True}
        if path == "/api/inbox/threads/9/generate-reply":
            return {"approval_id": 7}
        raise AssertionError(path)

    monkeypatch.setattr(MCP, "_request", request)
    reply = MCP.handle({"jsonrpc": "2.0", "id": 17, "method": "tools/call", "params": {
        "name": "ea_generate_inbox_reply", "arguments": {
            "thread_id": 9, "user_authorized": True,
            "authorization_source": "agent_takeover", "takeover_token": "capability",
        },
    }})
    assert reply["result"].get("isError") is not True
    assert calls[0] == ("POST", "/api/agent-takeover/authorize", {
        "token": "capability", "operation": "generate_inbox_reply", "thread_id": 9,
    })
    assert any(path == "/api/inbox/threads/9/generate-reply" for _, path, _ in calls)


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
