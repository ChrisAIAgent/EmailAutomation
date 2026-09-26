"""Phase 6 acceptance: lead-gen email generation.

Human-review coverage remains draft-only. Agent-review coverage is added below
for first-email auto-dispatch. The suite verifies: per-contact independent content, truthful (no fabrication),
correct sender signature, run metadata recorded (model/latency/prompt_version),
status pending, and no Gmail send during generation.
"""
from app.services import quality as quality_svc
from app.agents.langgraph_agent import PROMPT_VERSION


def _make_campaign(client, **over):
    payload = {
        "name": "Phase6", "agent_mode": "langgraph_only", "primary_agent": "langgraph",
        "product_description": "An AI grading tool for English teachers.",
        "objective": "Help teachers cut grading time by 70%.",
        "target_audience": "English training centers",
        "sender_name": "Zoe", "sender_company": "Zoe English Lab",
        "tone": "professional",
    }
    payload.update(over)
    r = client.post("/api/campaigns", json=payload)
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    started = client.post(f"/api/campaigns/{cid}/start")
    assert started.status_code == 200, started.text
    return cid


def _import(client, cid, csv_text):
    r = client.post(f"/api/campaigns/{cid}/import-csv", json={
        "campaign_id": cid, "csv_text": csv_text, "field_map": {}, "has_header": True,
    })
    assert r.status_code == 200, r.text
    return r.json()


def test_each_contact_gets_independent_content(client):
    cid = _make_campaign(client)
    _import(client, cid,
            "email,first_name,company,title\n"
            "a@test.com,Alice,Acme Inc,CTO\n"
            "b@test.com,Bob,Beta LLC,Manager\n")
    r = client.post(f"/api/campaigns/{cid}/generate")
    assert r.status_code == 200, r.text
    assert r.json()["generated"] == 2
    aps = client.get("/api/approvals", params={"status": "pending"}).json()
    bodies = {a["to_email"]: a["body_text"] for a in aps}
    subs = {a["to_email"]: a["subject"] for a in aps}
    # independent: different recipient name + company appear in each
    assert "Alice" in bodies["a@test.com"] and "Acme" in bodies["a@test.com"]
    assert "Bob" in bodies["b@test.com"] and "Beta" in bodies["b@test.com"]
    assert bodies["a@test.com"] != bodies["b@test.com"]
    assert subs["a@test.com"] != subs["b@test.com"]


def test_no_fabrication_when_company_missing(client):
    cid = _make_campaign(client)
    # contact with NO company -> template must fall back to "your team", never the sender's company
    _import(client, cid, "email,first_name\nnoinfo@test.com,Carol\n")
    client.post(f"/api/campaigns/{cid}/generate")
    a = client.get("/api/approvals", params={"status": "pending"}).json()[0]
    body = a["body_text"]
    assert "Zoe English Lab" not in body.replace("Zoe", "") or "your team" in body
    # the sender company must only appear in the signature, not as the recipient's company
    assert "good fit for Zoe English Lab" not in body
    assert a["quality"]["fabrication"] is False


def test_sender_signature_correct(client):
    cid = _make_campaign(client)
    _import(client, cid, "email,first_name,company\n"
            "sig@test.com,Dave,DaveCo\n")
    client.post(f"/api/campaigns/{cid}/generate")
    a = client.get("/api/approvals", params={"status": "pending"}).json()[0]
    body = a["body_text"]
    # Sender identity now comes from the Agent Profile (Sendy / TAC AISolution),
    # not the campaign's sender_name/sender_company (ignored unless
    # allow_campaign_override is enabled). This matches the reply path and the
    # AGENTS.md signature-post-processing contract.
    assert "Sendy" in body
    assert "TAC AISolution" in body
    # product + objective referenced
    assert "AI grading" in body
    assert "cut grading time" in body


def test_run_metadata_recorded(client):
    cid = _make_campaign(client)
    _import(client, cid, "email,first_name\n"
            "meta@test.com,Eve\n")
    client.post(f"/api/campaigns/{cid}/generate")
    a = client.get("/api/approvals", params={"status": "pending"}).json()[0]
    # draft-only / rule-based still records a model label, prompt version, latency
    assert a["prompt_version"] == PROMPT_VERSION
    assert a["latency_ms"] is not None
    assert isinstance(a["model"], str)
    # AgentRun row also records them
    from app.db import SessionLocal
    from app import models
    db = SessionLocal()
    run = db.query(models.AgentRun).filter_by(run_id=a["agent_run_id"]).first()
    assert run is not None
    assert run.model == a["model"]
    assert run.prompt_version == PROMPT_VERSION
    assert run.latency_ms == a["latency_ms"]
    db.close()


def test_status_pending_and_no_send(client):
    cid = _make_campaign(client)
    _import(client, cid, "email,first_name\n"
            "pend@test.com,Frank\n")
    client.post(f"/api/campaigns/{cid}/generate")
    aps = client.get("/api/approvals", params={"status": "pending"}).json()
    assert len(aps) == 1
    assert aps[0]["status"] == "pending"
    # Dashboard: no send happened during generation (draft-only)
    m = client.get("/api/dashboard/metrics").json()
    assert m["sent_today"] == 0
    assert m["draft_only"] is True


def test_agent_review_auto_dispatches_new_first_email(client, monkeypatch):
    from app.tools.email_tools import UnifiedEmailToolLayer

    cid = _make_campaign(client)
    _import(client, cid, "email,first_name\nauto@test.com,Auto\n")
    mode = client.put("/api/agent-profile/approval-mode", json={"approval_mode": "agent_review"})
    assert mode.status_code == 200, mode.text

    sent = []

    def fake_send(self, **kwargs):
        sent.append(kwargs)
        return {"ok": True, "message_id": "agent-review-message"}

    monkeypatch.setattr(UnifiedEmailToolLayer, "send_approved_draft", fake_send)
    generated = client.post(f"/api/campaigns/{cid}/generate")
    assert generated.status_code == 200, generated.text
    body = generated.json()
    assert body["generated"] == 1
    assert body["sent"] == 1
    assert body["send_failed"] == 0
    assert body["send_status"] == "completed"
    assert len(sent) == 1
    assert client.get("/api/approvals", params={"status": "pending"}).json() == []
    approved = client.get("/api/approvals", params={"status": "approved"}).json()
    assert len(approved) == 1
    assert approved[0]["kind"] == "first_send"


def test_agent_review_does_not_retroactively_send_existing_pending(client, monkeypatch):
    from app.tools.email_tools import UnifiedEmailToolLayer

    cid = _make_campaign(client)
    _import(client, cid, "email,first_name\nexisting@test.com,Existing\n")
    first = client.post(f"/api/campaigns/{cid}/generate")
    assert first.status_code == 200, first.text
    pending_before = client.get("/api/approvals", params={"status": "pending"}).json()
    assert len(pending_before) == 1

    sent = []

    def fake_send(self, **kwargs):
        sent.append(kwargs)
        return {"ok": True, "message_id": "should-not-send"}

    monkeypatch.setattr(UnifiedEmailToolLayer, "send_approved_draft", fake_send)
    mode = client.put("/api/agent-profile/approval-mode", json={"approval_mode": "agent_review"})
    assert mode.status_code == 200, mode.text
    pending_after = client.get("/api/approvals", params={"status": "pending"}).json()
    assert [item["id"] for item in pending_after] == [pending_before[0]["id"]]
    assert sent == []


def test_generation_keeps_success_when_one_contact_fails(client, monkeypatch):
    from app.api import campaigns as campaign_api
    from app.exceptions import AgentUnavailableError

    cid = _make_campaign(client)
    _import(client, cid, "email,first_name\nfirst@test.com,First\nsecond@test.com,Second\n")
    original = campaign_api.Orchestrator.generate_outreach

    def one_failure(self, inp):
        if inp.contact_id == 2:
            raise AgentUnavailableError("langgraph")
        return original(self, inp)

    monkeypatch.setattr(campaign_api.Orchestrator, "generate_outreach", one_failure)
    generated = client.post(f"/api/campaigns/{cid}/generate")
    assert generated.status_code == 200, generated.text
    body = generated.json()
    assert body["status"] == "partial"
    assert body["generated"] == 1
    assert body["failed"] == 1
    assert body["approvals"]
    assert body["failures"][0]["email"] == "second@test.com"
    assert len(client.get("/api/approvals", params={"status": "pending"}).json()) == 1
    assert client.get(f"/api/campaigns/{cid}/generation-status").json()["status"] == "partial"


def test_generation_reuses_existing_running_run(client, db):
    from app import models

    cid = _make_campaign(client)
    running = models.CampaignGenerationRun(campaign_id=cid, total_contacts=2)
    db.add(running)
    db.commit()
    response = client.post(f"/api/campaigns/{cid}/generate")
    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert response.json()["reused"] is True
    assert response.json()["run_id"] == running.id


def test_generation_releases_agent_decision_lock_before_gmail_draft(client, monkeypatch):
    """OAuth persistence in another Session must not block on AgentRun flushes."""
    from types import SimpleNamespace

    from app import models
    from app.api import campaigns as campaign_api
    from app.db import SessionLocal
    from app.schemas import EmailProposal

    cid = _make_campaign(client)
    _import(client, cid, "email,first_name\ndecision-lock@example.com,Decision\n")

    def fake_generate(self, inp):
        self.db.add(models.AgentRun(
            run_id=f"decision-lock-{inp.contact_id}", agent="langgraph",
            task_type="generate_outreach", campaign_id=inp.campaign_id,
            contact_id=inp.contact_id,
        ))
        self.db.flush()
        return EmailProposal(
            agent="langgraph", run_id=f"decision-lock-{inp.contact_id}",
            subject="APSARA invitation",
            body_text="Hello,\n\nPlease join us.\n\nBest regards,\nChris\nTAC AISolution",
            confidence=0.9,
        )

    def fake_create_draft(_db, _membership, _proposal, **_kwargs):
        oauth_session = SessionLocal()
        try:
            oauth_session.connection().exec_driver_sql("PRAGMA busy_timeout=1")
            oauth_session.add(models.AuditLog(
                actor="system", action="oauth_refresh_persist_test",
                entity="oauth_credential", entity_id="test", detail="",
            ))
            oauth_session.commit()
        finally:
            oauth_session.close()
        return SimpleNamespace(id=987)

    monkeypatch.setattr(campaign_api.Orchestrator, "generate_outreach", fake_generate)
    monkeypatch.setattr(campaign_api.approval_svc, "create_outreach_approval", fake_create_draft)

    response = client.post(f"/api/campaigns/{cid}/generate")

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["generated"] == 1
    assert response.json()["failed"] == 0


def test_quality_flags_fabrication():
    # Unit check: detection is deterministic and cannot be fooled by missing fields.
    fabricated = quality_svc.compute_quality(
        "Following up", "Hi Frank, as we discussed on our call, your team at Acme loved the demo.",
        contact={"first_name": "Frank", "company": "Acme", "title": "PM"},
        campaign={"product_description": "x", "objective": "y", "target_audience": "z", "sender_company": "UsCorp"},
    )
    assert fabricated["fabrication"] is True

    clean = quality_svc.compute_quality(
        "Frank, a quick idea for your team",
        "Hi Frank,\n\nI'm Zoe from Zoe English Lab. An AI grading tool for English teachers.\n\n"
        "We work with English training centers like Acme, and as PM you might find this a good fit.\n\n"
        "Would you be open to a short chat this week?\n\nBest,\nZoe\nZoe English Lab",
        contact={"first_name": "Frank", "company": "Acme", "title": "PM"},
        campaign={"product_description": "An AI grading tool for English teachers.",
                  "objective": "Help teachers cut grading time by 70%.",
                  "target_audience": "English training centers", "sender_company": "Zoe English Lab"},
    )
    assert clean["fabrication"] is False
    assert clean["personalization"] >= 4  # uses Frank + Acme + PM
    assert clean["business_relevance"] >= 3
    assert clean["cta"] >= 3
