"""Agent schema validation, OpenClaw failure handling, and compare-mode policy."""
import os
from app.config import get_settings
from app.agents.langgraph_agent import LangGraphAdapter
from app.agents.openclaw_adapter import OpenClawAdapter
from app.schemas import AnalyzeMessageInput, AgentDecision
from app.policy.engine import evaluate, PolicyContext
from app import models


def _setenv(**kw):
    for k, v in kw.items():
        os.environ[k] = v
    get_settings.cache_clear()


def test_langgraph_produces_valid_decision(db):
    _setenv(OPENAI_API_KEY="")  # force heuristic path
    adapter = LangGraphAdapter(db)
    d = adapter.analyze_message(AnalyzeMessageInput(subject="Interested", body_text="Yes I am interested, what is the price?"))
    assert isinstance(d, AgentDecision)
    assert 0.0 <= d.confidence <= 1.0
    assert d.intent in {"interested", "asking_question", "unknown"}
    assert d.requires_approval is True
    assert d.reasoning_summary  # short, displayable


def test_langgraph_intent_unsubscribe(db):
    _setenv(OPENAI_API_KEY="")
    adapter = LangGraphAdapter(db)
    d = adapter.analyze_message(AnalyzeMessageInput(subject="x", body_text="please unsubscribe me"))
    assert d.intent == "unsubscribe"
    assert d.recommended_action == "stop"


def test_langgraph_explicit_opt_out_wins_over_positive_language(db):
    _setenv(OPENAI_API_KEY="")
    adapter = LangGraphAdapter(db)
    d = adapter.analyze_message(AnalyzeMessageInput(
        subject="Interested",
        body_text="I am interested, but please do not send further follow-ups for now.",
    ))
    assert d.intent == "opt_out"
    assert d.recommended_action == "stop"


def test_quote_contamination_does_not_false_unsubscribe():
    """A customer reply that quotes our own outreach (whose body mentions
    'unsubscribe/bounce suppression management') must NOT be classified as an
    opt-out. Intent detection strips quoted/forwarded text first (Fix A)."""
    from app.agents.langgraph_agent import _strip_quoted_text, _opt_out_intent

    reply = (
        "Yes Please，share more relevant details， will take a quick look\r\n\r\n"
        "On Wed, Aug 12, 2026 at 5:43 PM S TAC <tac.aisolution@gmail.com> wrote:\r\n\r\n"
        "> Hi Ziyi,\r\n> Our offering includes automated follow-up with smart stop "
        "rules, and unsubscribe/bounce suppression management, designed to help you.\r\n"
    )
    # Without stripping, the quoted product copy triggers a false opt-out.
    assert _opt_out_intent(reply) is not None
    # After stripping quoted text, only the customer's own words remain.
    stripped = _strip_quoted_text(reply)
    assert "unsubscribe" not in stripped.lower()
    assert _opt_out_intent(stripped) is None


def test_openclaw_unreachable_returns_none_not_fake(db):
    _setenv(OPENCLAW_TRANSPORT="http", OPENCLAW_ENDPOINT="http://127.0.0.1:9/task", OPENCLAW_TIMEOUT_SECONDS="1", OPENCLAW_API_KEY="")
    adapter = OpenClawAdapter(db)
    # health should report configured but unreachable
    h = adapter.health_check()
    assert h.configured is True
    # analyze must NOT produce a fake decision; returns None
    d = adapter.analyze_message(AnalyzeMessageInput(subject="hi", body_text="hello"))
    assert d is None
    # a failed AgentRun must be recorded (no silent fake)
    failed = db.query(models.AgentRun).filter_by(agent="openclaw", status="failed").first()
    assert failed is not None


def test_openclaw_validation_rejects_bad_output(db):
    _setenv(OPENCLAW_TRANSPORT="http", OPENCLAW_ENDPOINT="http://127.0.0.1:9/task")
    adapter = OpenClawAdapter(db)
    bad = {"intent": "not_a_real_intent", "confidence": "high", "extra": 1}
    assert adapter._validate(bad) is None
    good = {
        "intent": "interested", "confidence": 0.9, "summary": "x",
        "recommended_action": "human_review", "reasoning_summary": "r",
        "risk_level": "low", "requires_approval": True,
        "draft": {"subject": "s", "body_text": "b", "body_html": ""},
        "tool_requests": [], "model": "m", "prompt_version": "1.0",
    }
    assert adapter._validate(good) is not None


def test_compare_shadow_cannot_execute_gmail(db):
    # Shadow agent (compare mode, is_primary=False) must be blocked from Gmail writes.
    for tool in ["create_draft", "update_draft", "send_approved_draft", "add_label", "schedule_follow_up"]:
        res = evaluate(db, PolicyContext(
            agent="openclaw", mode="compare", is_primary=False, tool_name=tool,
            to_email="x@x.com", campaign_id=1,
        ))
        assert res.allowed is False, f"{tool} should be blocked for shadow"
        assert "shadow" in (res.reason or "").lower()


def test_primary_can_create_draft_in_compare(db):
    camp = models.Campaign(owner_id=1, name="C", status="active")
    db.add(camp)
    db.commit()
    res = evaluate(db, PolicyContext(
        agent="langgraph", mode="compare", is_primary=True, tool_name="create_draft",
        to_email="x@x.com", campaign_id=camp.id,
    ))
    assert res.allowed is True
