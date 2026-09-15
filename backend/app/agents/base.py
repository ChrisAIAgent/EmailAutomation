"""Unified Agent Adapter interface.

Both LangGraphAgent and OpenClawAdapter implement this. They return the SAME
structured result (AgentDecision / EmailProposal). They NEVER call Gmail — all
mail ops go through the Unified Email Tool Layer via the orchestrator/services.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..schemas import (
    AgentDecision,
    AgentHealth,
    EmailProposal,
    ApprovalRevisionInput,
    AnalyzeMessageInput,
    GenerateOutreachInput,
    GenerateFollowUpInput,
    PlanNextActionInput,
)


class AgentAdapter(ABC):
    agent_name: str = "base"

    @abstractmethod
    def analyze_message(self, inp: AnalyzeMessageInput) -> Optional[AgentDecision]: ...

    @abstractmethod
    def generate_outreach(self, inp: GenerateOutreachInput) -> Optional[EmailProposal]: ...

    @abstractmethod
    def generate_follow_up(self, inp: GenerateFollowUpInput) -> Optional[EmailProposal]: ...

    @abstractmethod
    def revise_approval(self, inp: ApprovalRevisionInput) -> Optional[EmailProposal]: ...

    @abstractmethod
    def plan_next_action(self, inp: PlanNextActionInput) -> Optional[AgentDecision]: ...

    @abstractmethod
    def health_check(self) -> AgentHealth: ...
