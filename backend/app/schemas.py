"""Pydantic schemas: API I/O and the unified Agent contract."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field


# ---------------------------------------------------------------------------
# Unified Agent interface
# ---------------------------------------------------------------------------
class DraftPayload(BaseModel):
    subject: str
    body_text: str
    body_html: str = ""


class AgentDecision(BaseModel):
    """Structured decision returned by EVERY agent. No hidden chain-of-thought."""

    agent: str  # langgraph | openclaw
    run_id: str
    intent: str  # interested | asking_question | objection | not_interested | unsubscribe | opt_out | out_of_office | bounce | unknown
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    recommended_action: str  # reply | follow_up_later | stop | human_review | no_action
    reasoning_summary: str = Field(
        ..., description="Short, displayable rationale. Never a hidden CoT."
    )
    risk_level: str = Field(default="low")  # low | medium | high
    requires_approval: bool = True
    suggested_follow_up_at: Optional[str] = None  # ISO datetime or null
    draft: Optional[DraftPayload] = None
    tool_requests: list[Any] = Field(default_factory=list)
    latency_ms: int = 0
    model: str = ""
    prompt_version: str = "1.0"

    def confidence_pct(self) -> int:
        return int(self.confidence * 100)


class EmailProposal(BaseModel):
    """Alias returned by generateOutreach / generateFollowUp."""

    agent: str
    run_id: str
    subject: str
    body_text: str
    body_html: str = ""
    intent: str = "unknown"
    confidence: float = 0.0
    summary: str = ""
    recommended_action: str = "human_review"
    reasoning_summary: str = ""
    risk_level: str = "low"
    requires_approval: bool = True
    latency_ms: int = 0
    model: str = ""
    prompt_version: str = "1.0"


class AgentHealth(BaseModel):
    agent: str
    configured: bool
    reachable: bool = True
    mode: Optional[str] = None
    detail: str = ""
    latency_ms: Optional[int] = None


def decision_to_proposal(d: "AgentDecision") -> EmailProposal:
    """Convert an AgentDecision (compare mode) into the EmailProposal shape used by approvals."""
    subj = d.draft.subject if d.draft else ""
    body = d.draft.body_text if d.draft else ""
    html = d.draft.body_html if d.draft else ""
    return EmailProposal(
        agent=d.agent, run_id=d.run_id, subject=subj, body_text=body, body_html=html,
        intent=d.intent, confidence=d.confidence, summary=d.summary,
        recommended_action=d.recommended_action, reasoning_summary=d.reasoning_summary,
        risk_level=d.risk_level, requires_approval=d.requires_approval,
        latency_ms=d.latency_ms, model=d.model, prompt_version=d.prompt_version,
    )


# Inputs
class AnalyzeMessageInput(BaseModel):
    message_id: Optional[str] = None
    thread_id: Optional[int] = None
    subject: Optional[str] = None
    body_text: Optional[str] = None
    from_email: Optional[str] = None
    campaign_id: Optional[int] = None
    contact_id: Optional[int] = None
    thread_context: Optional[str] = None
    mode: str = "langgraph_only"


class GenerateOutreachInput(BaseModel):
    campaign_id: int
    contact_id: int
    mode: str = "langgraph_only"
    custom_instructions: Optional[str] = None


class GenerateFollowUpInput(BaseModel):
    campaign_id: int
    contact_id: int
    thread_id: int
    sequence: int = 1
    mode: str = "langgraph_only"


class ApprovalRevisionInput(BaseModel):
    """Internal, draft-only instruction for revising one pending Approval."""

    approval_id: int
    instruction: str
    kind: str
    current_subject: str
    current_body_text: str
    campaign_id: Optional[int] = None
    contact_id: Optional[int] = None
    thread_id: Optional[int] = None
    thread_context: Optional[str] = None
    latest_customer_message: Optional[str] = None
    intent: str = "unknown"
    recommended_action: str = "human_review"
    risk_level: str = "low"
    mode: str = "langgraph_only"
    last_message_body: Optional[str] = None


class PlanNextActionInput(BaseModel):
    campaign_id: int
    contact_id: int
    thread_id: int
    last_intent: Optional[str] = None
    mode: str = "langgraph_only"


# ---------------------------------------------------------------------------
# Campaign / Contact
# ---------------------------------------------------------------------------
class CampaignBase(BaseModel):
    name: str
    objective: Optional[str] = None
    product_description: Optional[str] = None
    target_audience: Optional[str] = None
    sender_name: Optional[str] = None
    sender_company: Optional[str] = None
    tone: str = "professional"
    primary_agent: str = "langgraph"
    agent_mode: str = "langgraph_only"
    daily_send_limit: int = Field(default=20, ge=1)
    sending_window_start: int = Field(default=9, ge=0, le=23)
    sending_window_end: int = Field(default=18, ge=1, le=24)
    timezone: str = "Asia/Shanghai"
    max_follow_ups: int = Field(default=2, ge=0)
    follow_up_intervals_days: str = "3,4"
    approval_mode: str = "human_confirm"
    confidence_threshold: int = 70


class CampaignCreate(CampaignBase):
    pass


class CampaignOut(CampaignBase):
    id: int
    owner_id: int
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ContactBase(BaseModel):
    email: EmailStr
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    title: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    category: str = "prospect"
    tags: list[str] = Field(default_factory=list)
    segments: list[str] = Field(default_factory=list)
    intent_level: str = "unknown"
    notes: Optional[str] = None
    lifecycle_stage: str = "new_customer"
    next_action: Optional[str] = None
    manual_lock: bool = False
    custom_fields: Optional[dict] = None
    source: Optional[str] = None
    timezone: Optional[str] = None


class ContactCreate(ContactBase):
    pass


class ContactUpdate(BaseModel):
    """Partial CRM update.

    Every field is optional so omitted values remain unchanged. Explicit nulls
    still clear nullable fields.
    """

    email: Optional[EmailStr] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    title: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[list[str]] = None
    segments: Optional[list[str]] = None
    intent_level: Optional[str] = None
    notes: Optional[str] = None
    lifecycle_stage: Optional[str] = None
    next_action: Optional[str] = None
    manual_lock: Optional[bool] = None
    custom_fields: Optional[dict] = None
    source: Optional[str] = None
    timezone: Optional[str] = None
    # Request metadata used for the Contact AuditLog; these are not persisted
    # as Contact columns and preserve the partial-update contract.
    reason: Optional[str] = None
    override_manual_lock: bool = False


class ContactTransition(BaseModel):
    action: str
    campaign_id: Optional[int] = None
    intent: Optional[str] = None
    reason: Optional[str] = None
    override_manual_lock: bool = False


class ContactOut(ContactBase):
    id: int
    owner_id: int
    status: str
    last_contacted_at: Optional[datetime] = None
    next_follow_up_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class CsvImportRequest(BaseModel):
    campaign_id: int
    csv_text: str
    field_map: dict[str, str]
    has_header: bool = True
    skip_allowlist_check: bool = False


class CsvImportResult(BaseModel):
    total_rows: int
    imported: int
    duplicates_skipped: int
    invalid_email: int
    suppressed_skipped: int
    rejected_rows: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Approval / Send
# ---------------------------------------------------------------------------
class ApprovalDecision(BaseModel):
    decision: str  # approve | reject
    editor_email: Optional[str] = None
    edited_subject: Optional[str] = None
    edited_body_text: Optional[str] = None
    edited_body_html: Optional[str] = None
    rejection_reason: Optional[str] = None


class ApprovalInvalidation(BaseModel):
    """Close a pending approval without sending its draft."""
    reason: str = "invalidated by operator"
    editor_email: Optional[str] = None


class ApprovalRevision(BaseModel):
    """A natural-language instruction to revise, but never dispatch, one draft."""

    instruction: str = Field(min_length=1, max_length=2_000)
    editor_email: Optional[str] = Field(default=None, max_length=200)

class SendResult(BaseModel):
    draft_id: int
    message_id: Optional[str] = None
    status: str
    idempotency_key: str
    detail: str = ""


# ---------------------------------------------------------------------------
# Dashboard / Activity
# ---------------------------------------------------------------------------
class DashboardMetrics(BaseModel):
    connected_emails: int
    active_campaigns: int
    sent_today: int
    replies: int
    positive_replies: int
    pending_approvals: int
    scheduled_follow_ups: int
    failed_tasks: int
    real_send_enabled: bool
    draft_only: bool
    openclaw_connected: bool
    langgraph_configured: bool


class ActivityItem(BaseModel):
    id: int
    category: str
    created_at: datetime
    summary: str
    status: Optional[str] = None
    detail: Optional[str] = None


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
class ComparisonView(BaseModel):
    comparison_key: str
    task_type: str
    thread_id: Optional[int] = None
    langgraph: Optional[AgentDecision] = None
    openclaw: Optional[AgentDecision] = None
    intent_agree: Optional[bool] = None
    action_agree: Optional[bool] = None
    selected: Optional[str] = None
    adopted: Optional[bool] = None


class ComparisonSelection(BaseModel):
    selected: str  # langgraph | openclaw | edited | none
    edited_subject: Optional[str] = None
    edited_body_text: Optional[str] = None
    edited_body_html: Optional[str] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# OAuth / Gmail
# ---------------------------------------------------------------------------
class GmailStatus(BaseModel):
    connected: bool
    email: Optional[str] = None
    granted_scopes: Optional[list[str]] = None
    history_id: Optional[str] = None
    is_demo: bool = False


class OAuthStart(BaseModel):
    url: str
    state: str


# ---------------------------------------------------------------------------
# Automation (natural-language driven email automation)
# ---------------------------------------------------------------------------
class AutomationPlan(BaseModel):
    """Structured, safe configuration derived from a natural-language prompt.

    Only configuration values (intervals, limits, intent lists) 鈥?never customer
    data. Conservative defaults are applied for any field the prompt does not set.
    """

    enabled: bool = False
    tick_interval_minutes: int = 5
    first_email_approval_required: bool = True
    follow_up_after_days: int = 3
    max_follow_ups: int = 2
    auto_send_low_risk_follow_up: bool = False
    execution_mode: str = "full_auto"
    takeover_scope: Optional[str] = None
    takeover_days: Optional[int] = None
    takeover_cutoff_at: Optional[str] = None
    stop_on_intents: list[str] = Field(
        default_factory=lambda: ["unsubscribe", "opt_out", "not_interested", "bounce"]
    )
    daily_send_limit: int = 1
    sending_window: dict = Field(
        default_factory=lambda: {"start": 9, "end": 18, "tz": "Asia/Shanghai"}
    )


class AutomationGenerateRequest(BaseModel):
    prompt: str
    campaign_id: Optional[int] = None
    scope: str = "campaign"


class AutomationCreate(BaseModel):
    prompt: str
    campaign_id: Optional[int] = None
    scope: str = "campaign"
    plan: AutomationPlan
    name: Optional[str] = None


class GlobalAutomationUpdate(BaseModel):
    enabled: bool
    mode: str = "full_auto"
    tick_interval_minutes: int = 60
    takeover_scope: Optional[str] = None
    takeover_days: Optional[int] = None


class AutomationScheduleUpdate(BaseModel):
    """Web-managed cadence for the backend/Huey scheduler."""

    tick_interval_minutes: int = Field(ge=1, le=1440)


class AutomationOut(BaseModel):
    id: int
    owner_id: int
    name: str
    prompt: str
    campaign_id: Optional[int] = None
    scope: str = "campaign"
    status: str
    plan: AutomationPlan
    tick_interval_minutes: int
    next_run_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    last_status: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AutomationRunOut(BaseModel):
    id: int
    automation_id: int
    trigger: str
    source: str
    status: str
    synced_threads: int = 0
    approvals_created: int = 0
    drafts_created: int = 0
    follow_ups_resolved: int = 0
    replies_stopped: int = 0
    summary: Optional[str] = None
    timeline: list = Field(default_factory=list)
    error: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AgentRunCreate(BaseModel):
    automation_id: int
    mode: str = "full_auto"


class AgentRunConfirm(BaseModel):
    confirmed_by: Optional[str] = None


# ---------------------------------------------------------------------------
# P1-7: read-endpoint response contracts. Permissive (extra allowed) so nested
# fields (plan, delivery, quality, timeline, send_plan) survive serialization
# while still giving the Agent a stable OpenAPI schema to introspect.
# ---------------------------------------------------------------------------
class _ExtraBase(BaseModel):
    model_config = {"extra": "allow"}


class ApprovalOut(_ExtraBase):
    id: int
    kind: Optional[str] = None
    status: Optional[str] = None
    to_email: Optional[str] = None
    subject: Optional[str] = None
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    recommended_action: Optional[str] = None
    risk_level: Optional[str] = None
    campaign_id: Optional[int] = None
    thread_id: Optional[int] = None
    quality: Any = None
    agent_run_id: Optional[str] = None
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    latency_ms: Optional[int] = None
    created_at: Any = None
    decided_at: Any = None
    delivery: Any = None
    contact_name: Any = None
    contact_company: Any = None


class AutomationItemOut(_ExtraBase):
    id: int
    owner_id: Optional[int] = None
    name: Optional[str] = None
    prompt: Optional[str] = None
    campaign_id: Optional[int] = None
    scope: Optional[str] = None
    status: Optional[str] = None
    plan: Any = None
    execution_mode: Optional[str] = None
    tick_interval_minutes: Optional[int] = None
    next_run_at: Any = None
    last_run_at: Any = None
    last_status: Optional[str] = None
    created_at: Any = None


class AutomationListOut(_ExtraBase):
    items: list[AutomationItemOut] = Field(default_factory=list)
    openclaw_cron_configured: bool = False
    openclaw_configured: bool = False


class AgentRunOut(_ExtraBase):
    id: int
    automation_id: Optional[int] = None
    mode: Optional[str] = None
    status: Optional[str] = None
    summary: Optional[str] = None
    error: Any = None
    prepared_at: Any = None
    confirmed_at: Any = None
    confirmed_by: Optional[str] = None
    confirmation_expires_at: Any = None
    send_plan: Any = None
