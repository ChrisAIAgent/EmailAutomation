"""Agent Orchestrator: routes tasks across LangGraph / OpenClaw and the compare mode.

Safety: in compare mode, the PRIMARY agent (campaign.primary_agent) may proceed
to approval/execution; the SHADOW agent only produces decisions/drafts and must
never execute Gmail writes (enforced downstream at the tool layer).
"""
from __future__ import annotations

import json
import hashlib
from typing import Optional

from .. import models
from ..config import get_settings
from ..schemas import (
    AgentDecision,
    AgentHealth,
    ComparisonView,
    EmailProposal,
    AnalyzeMessageInput,
    GenerateOutreachInput,
    GenerateFollowUpInput,
    ApprovalRevisionInput,
    PlanNextActionInput,
)
from .base import AgentAdapter
from .langgraph_agent import LangGraphAdapter
from .openclaw_adapter import OpenClawAdapter


class Orchestrator:
    def __init__(self, db):
        self.db = db

    def _make(self, agent: str) -> AgentAdapter:
        if agent == "openclaw":
            return OpenClawAdapter(self.db)
        return LangGraphAdapter(self.db)

    def _primary_agent(self, campaign_id: Optional[int]) -> str:
        if campaign_id:
            c = self.db.get(models.Campaign, campaign_id)
            if c and c.primary_agent in ("langgraph", "openclaw"):
                return c.primary_agent
        return "langgraph"

    # ---- recording ----
    def _record_decision(self, agent, mode, decision, inp, task_type) -> Optional[models.AgentRun]:
        if decision is None:
            return None
        campaign_id = getattr(inp, "campaign_id", None)
        contact_id = getattr(inp, "contact_id", None)
        thread_id = getattr(inp, "thread_id", None)
        run = models.AgentRun(
            run_id=decision.run_id,
            agent=agent,
            mode=mode,
            task_type=task_type,
            campaign_id=campaign_id,
            contact_id=contact_id,
            thread_id=thread_id,
            intent=decision.intent,
            confidence=int(decision.confidence * 100),
            recommended_action=decision.recommended_action,
            risk_level=decision.risk_level,
            requires_approval=decision.requires_approval,
            summary=decision.summary,
            reasoning_summary=decision.reasoning_summary,  # short, no hidden CoT
            model=decision.model,
            prompt_version=decision.prompt_version,
            latency_ms=decision.latency_ms,
            status="success",
            result_json=json.dumps(decision.model_dump(), ensure_ascii=False),
        )
        self.db.add(run)
        self.db.flush()
        return run

    def _store_comparison(self, task_type, key, lg: Optional[AgentDecision], oc: Optional[AgentDecision], campaign_id=None, thread_id=None, contact_id=None) -> models.AgentComparison:
        cmp = models.AgentComparison(
            comparison_key=key,
            task_type=task_type,
            campaign_id=campaign_id,
            thread_id=thread_id,
            contact_id=contact_id,
            langgraph_run_id=lg.run_id if lg else None,
            openclaw_run_id=oc.run_id if oc else None,
            intent_agree=bool(lg and oc and lg.intent == oc.intent),
            action_agree=bool(lg and oc and lg.recommended_action == oc.recommended_action),
        )
        self.db.add(cmp)
        self.db.flush()
        return cmp

    # ---- public API ----
    def analyze(self, inp: AnalyzeMessageInput) -> AgentDecision | ComparisonView:
        mode = inp.mode
        campaign_id = inp.campaign_id
        if mode == "compare":
            primary = self._primary_agent(campaign_id)
            lg = self._make("langgraph")
            oc = self._make("openclaw")
            d_lg = lg.analyze_message(inp)
            d_oc = oc.analyze_message(inp)
            self._record_decision("langgraph", mode, d_lg, inp, "analyze_message")
            self._record_decision("openclaw", mode, d_oc, inp, "analyze_message")
            key = f"analyze:{inp.thread_id or inp.message_id or 'x'}"
            cmp = self._store_comparison("analyze_message", key, d_lg, d_oc, campaign_id, inp.thread_id, inp.contact_id)
            return ComparisonView(
                comparison_key=key, task_type="analyze_message", thread_id=inp.thread_id,
                langgraph=d_lg, openclaw=d_oc,
                intent_agree=cmp.intent_agree, action_agree=cmp.action_agree,
                selected=cmp.selected, adopted=cmp.adopted,
            )
        agent = "openclaw" if mode == "openclaw_only" else "langgraph"
        adapter = self._make(agent)
        d = adapter.analyze_message(inp)
        self._record_decision(agent, mode, d, inp, "analyze_message")
        if d is None:
            from ..exceptions import AgentUnavailableError
            raise AgentUnavailableError(agent)
        return d

    def generate_outreach(self, inp: GenerateOutreachInput) -> EmailProposal | ComparisonView:
        mode = inp.mode
        if mode == "compare":
            lg = self._make("langgraph")
            oc = self._make("openclaw")
            p_lg = lg.generate_outreach(inp)
            p_oc = oc.generate_outreach(inp)
            self._record_decision("langgraph", mode, _as_decision(p_lg), inp, "generate_outreach")
            self._record_decision("openclaw", mode, _as_decision(p_oc), inp, "generate_outreach")
            key = f"outreach:{inp.campaign_id}:{inp.contact_id}"
            cmp = self._store_comparison("generate_outreach", key, _as_decision(p_lg), _as_decision(p_oc), inp.campaign_id, None, inp.contact_id)
            return ComparisonView(comparison_key=key, task_type="generate_outreach",
                                  langgraph=_as_decision(p_lg), openclaw=_as_decision(p_oc),
                                  intent_agree=cmp.intent_agree, action_agree=cmp.action_agree,
                                  selected=cmp.selected, adopted=cmp.adopted)
        agent = "openclaw" if mode == "openclaw_only" else "langgraph"
        adapter = self._make(agent)
        p = adapter.generate_outreach(inp)
        self._record_decision(agent, mode, _as_decision(p), inp, "generate_outreach")
        if p is None:
            from ..exceptions import AgentUnavailableError
            raise AgentUnavailableError(agent)
        return p

    def generate_follow_up(self, inp: GenerateFollowUpInput) -> EmailProposal | ComparisonView:
        mode = inp.mode
        if mode == "compare":
            lg = self._make("langgraph")
            oc = self._make("openclaw")
            p_lg = lg.generate_follow_up(inp)
            p_oc = oc.generate_follow_up(inp)
            self._record_decision("langgraph", mode, _as_decision(p_lg), inp, "generate_follow_up")
            self._record_decision("openclaw", mode, _as_decision(p_oc), inp, "generate_follow_up")
            key = f"followup:{inp.thread_id}:{inp.sequence}"
            cmp = self._store_comparison("generate_follow_up", key, _as_decision(p_lg), _as_decision(p_oc), inp.campaign_id, inp.thread_id, inp.contact_id)
            return ComparisonView(comparison_key=key, task_type="generate_follow_up",
                                  thread_id=inp.thread_id,
                                  langgraph=_as_decision(p_lg), openclaw=_as_decision(p_oc),
                                  intent_agree=cmp.intent_agree, action_agree=cmp.action_agree,
                                  selected=cmp.selected, adopted=cmp.adopted)
        agent = "openclaw" if mode == "openclaw_only" else "langgraph"
        adapter = self._make(agent)
        p = adapter.generate_follow_up(inp)
        self._record_decision(agent, mode, _as_decision(p), inp, "generate_follow_up")
        if p is None:
            from ..exceptions import AgentUnavailableError
            raise AgentUnavailableError(agent)
        return p

    def revise_approval(self, inp: ApprovalRevisionInput) -> EmailProposal | ComparisonView:
        """Revise an existing pending Approval without performing any Gmail action."""
        mode = inp.mode
        if mode == "compare":
            primary = self._primary_agent(inp.campaign_id)
            lg = self._make("langgraph")
            oc = self._make("openclaw")
            p_lg = lg.revise_approval(inp)
            p_oc = oc.revise_approval(inp)
            self._record_decision("langgraph", mode, _as_decision(p_lg), inp, "revise_approval")
            self._record_decision("openclaw", mode, _as_decision(p_oc), inp, "revise_approval")
            digest = hashlib.sha256(inp.instruction.encode("utf-8")).hexdigest()[:12]
            key = f"revision:{inp.approval_id}:{digest}"
            cmp = self._store_comparison(
                "revise_approval", key, _as_decision(p_lg), _as_decision(p_oc),
                inp.campaign_id, inp.thread_id, inp.contact_id,
            )
            return ComparisonView(
                comparison_key=key, task_type="revise_approval", thread_id=inp.thread_id,
                langgraph=_as_decision(p_lg), openclaw=_as_decision(p_oc),
                intent_agree=cmp.intent_agree, action_agree=cmp.action_agree,
                selected=cmp.selected or primary, adopted=cmp.adopted,
            )
        agent = "openclaw" if mode == "openclaw_only" else "langgraph"
        proposal = self._make(agent).revise_approval(inp)
        self._record_decision(agent, mode, _as_decision(proposal), inp, "revise_approval")
        if proposal is None:
            from ..exceptions import AgentUnavailableError
            raise AgentUnavailableError(agent)
        return proposal

    def plan_next_action(self, inp: PlanNextActionInput) -> AgentDecision | ComparisonView:
        mode = inp.mode
        if mode == "compare":
            lg = self._make("langgraph")
            oc = self._make("openclaw")
            d_lg = lg.plan_next_action(inp)
            d_oc = oc.plan_next_action(inp)
            self._record_decision("langgraph", mode, d_lg, inp, "plan_next_action")
            self._record_decision("openclaw", mode, d_oc, inp, "plan_next_action")
            key = f"plan:{inp.thread_id}"
            cmp = self._store_comparison("plan_next_action", key, d_lg, d_oc, inp.campaign_id, inp.thread_id, inp.contact_id)
            return ComparisonView(comparison_key=key, task_type="plan_next_action",
                                  thread_id=inp.thread_id,
                                  langgraph=d_lg, openclaw=d_oc,
                                  intent_agree=cmp.intent_agree, action_agree=cmp.action_agree,
                                  selected=cmp.selected, adopted=cmp.adopted)
        agent = "openclaw" if mode == "openclaw_only" else "langgraph"
        adapter = self._make(agent)
        d = adapter.plan_next_action(inp)
        self._record_decision(agent, mode, d, inp, "plan_next_action")
        if d is None:
            from ..exceptions import AgentUnavailableError
            raise AgentUnavailableError(agent)
        return d

    def health(self) -> list[AgentHealth]:
        lg = LangGraphAdapter(self.db).health_check()
        oc = OpenClawAdapter(self.db).health_check()
        return [lg, oc]


def _as_decision(p: Optional[EmailProposal]) -> Optional[AgentDecision]:
    if p is None:
        return None
    from ..schemas import DraftPayload
    return AgentDecision(
        agent=p.agent, run_id=p.run_id, intent=p.intent, confidence=p.confidence,
        summary=p.summary, recommended_action=p.recommended_action,
        reasoning_summary=p.reasoning_summary, risk_level=p.risk_level,
        requires_approval=p.requires_approval, suggested_follow_up_at=None,
        draft=DraftPayload(subject=p.subject, body_text=p.body_text, body_html=p.body_html) if p.subject else None,
        latency_ms=p.latency_ms, model=p.model, prompt_version=p.prompt_version,
    )
