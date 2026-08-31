"""SQLAlchemy ORM models. Compatible with SQLite (dev) and PostgreSQL (prod)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
    text,
)
from sqlalchemy.orm import relationship

from .db import Base


def _utcnow():
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(320), unique=True, index=True, nullable=False)
    name = Column(String(200), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)

    gmail_accounts = relationship("GmailAccount", back_populates="user")
    campaigns = relationship("Campaign", back_populates="owner")


class AgentProfile(TimestampMixin, Base):
    """Owner-level identity and behavior contract used on every Agent run."""

    __tablename__ = "agent_profiles"
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    agent_name = Column(String(200), default="Sendy", nullable=False)
    company_name = Column(String(200), default="TAC AISolution", nullable=False)
    role = Column(String(300), default="Enterprise AI Solutions Sales Consultant", nullable=False)
    tone = Column(String(100), default="professional, consultative, concise, friendly", nullable=False)
    language_policy = Column(String(40), default="match_customer", nullable=False)
    signature_text = Column(Text, default="Best regards,\nSendy\nTAC AISolution", nullable=False)
    forbidden_claims = Column(
        Text,
        default="pricing,delivery dates,guaranteed results,unapproved customer cases,compliance guarantees",
        nullable=False,
    )
    unknown_answer_policy = Column(
        Text,
        default="State that the detail needs confirmation and propose a concrete next step.",
        nullable=False,
    )
    allow_campaign_override = Column(Boolean, default=False, nullable=False)
    # Workspace-wide maximum send authority: human_review | agent_review.
    approval_mode = Column(String(20), default="human_review", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    version = Column(Integer, default=1, nullable=False)


class KnowledgeDocument(TimestampMixin, Base):
    """User-managed source material available to the production Agent."""

    __tablename__ = "knowledge_documents"
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(300), nullable=False)
    category = Column(String(80), default="general", nullable=False, index=True)
    content = Column(Text, nullable=False)
    source_type = Column(String(30), default="pasted", nullable=False)
    # pasted | markdown | text | csv
    source_name = Column(String(300), nullable=True)
    content_hash = Column(String(64), nullable=False)
    status = Column(String(20), default="draft", nullable=False, index=True)
    # draft | published | disabled (archived may exist only in legacy databases)
    version = Column(Integer, default=1, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "content_hash", name="uq_owner_knowledge_hash"),
    )


class GmailAccount(TimestampMixin, Base):
    __tablename__ = "gmail_accounts"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    email = Column(String(320), nullable=False, index=True)
    google_sub = Column(String(200), nullable=True, index=True)
    history_id = Column(String(50), nullable=True)
    # Capabilities granted (comma separated): read, draft, send
    granted_scopes = Column(String(500), nullable=True)
    is_connected = Column(Boolean, default=True, nullable=False)

    user = relationship("User", back_populates="gmail_accounts")
    oauth = relationship(
        "OAuthCredential", back_populates="account", uselist=False, cascade="all, delete"
    )
    threads = relationship("EmailThread", back_populates="account")


class OAuthCredential(TimestampMixin, Base):
    """Separate table for sensitive tokens. Stored encrypted at rest."""

    __tablename__ = "oauth_credentials"
    id = Column(Integer, primary_key=True)
    gmail_account_id = Column(
        Integer, ForeignKey("gmail_accounts.id"), nullable=False, unique=True, index=True
    )
    # Encrypted blobs (Fernet). Never returned to client, never logged in full.
    access_token_enc = Column(Text, nullable=True)
    refresh_token_enc = Column(Text, nullable=True)
    token_expiry = Column(DateTime(timezone=True), nullable=True)
    token_uri = Column(String(500), nullable=True)

    account = relationship("GmailAccount", back_populates="oauth")


class GmailSyncRun(TimestampMixin, Base):
    """Durable first-import/incremental synchronization state.

    Full mailbox imports are resumable background work. Incremental runs are
    recorded as well so the UI and Agent can report the exact synchronization
    semantics instead of treating a partial recent-page scan as complete.
    """

    __tablename__ = "gmail_sync_runs"
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    gmail_account_id = Column(
        Integer, ForeignKey("gmail_accounts.id"), nullable=False, index=True
    )
    kind = Column(String(30), nullable=False, index=True)  # initial_full | incremental
    status = Column(String(30), default="queued", nullable=False, index=True)
    # queued | running | paused | completed | failed | cancelled | cursor_expired
    query = Column(String(500), nullable=True)
    include_spam_trash = Column(Boolean, default=False, nullable=False)
    page_token = Column(Text, nullable=True)
    start_history_id = Column(String(50), nullable=True)
    latest_history_id = Column(String(50), nullable=True)
    threads_scanned = Column(Integer, default=0, nullable=False)
    new_threads = Column(Integer, default=0, nullable=False)
    new_messages = Column(Integer, default=0, nullable=False)
    failures = Column(Integer, default=0, nullable=False)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "uq_gmail_sync_inflight",
            gmail_account_id,
            unique=True,
            sqlite_where=text("status IN ('queued', 'running', 'paused')"),
            postgresql_where=text("status IN ('queued', 'running', 'paused')"),
        ),
    )


class InboxTriageRun(TimestampMixin, Base):
    """Durable Inbox triage work owned by one Workspace.

    The run is intentionally separate from Gmail synchronization and outbound
    automation: it only classifies a fixed local EmailThread snapshot.
    """

    __tablename__ = "inbox_triage_runs"
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    gmail_sync_run_id = Column(Integer, ForeignKey("gmail_sync_runs.id"), nullable=True, index=True)
    kind = Column(String(30), default="initial_history", nullable=False, index=True)
    # initial_history | daily_incremental
    status = Column(String(30), default="queued", nullable=False, index=True)
    # queued | running | paused | completed | failed | cancelled | recovery_pending
    batch_size = Column(Integer, default=50, nullable=False)
    total_threads = Column(Integer, default=0, nullable=False)
    processed_threads = Column(Integer, default=0, nullable=False)
    auto_filtered = Column(Integer, default=0, nullable=False)
    human_review = Column(Integer, default=0, nullable=False)
    business_threads = Column(Integer, default=0, nullable=False)
    no_action = Column(Integer, default=0, nullable=False)
    skipped_threads = Column(Integer, default=0, nullable=False)
    failed_threads = Column(Integer, default=0, nullable=False)
    current_batch = Column(Integer, default=0, nullable=False)
    last_progress_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(Text, nullable=True)

    __table_args__ = (
        Index(
            "uq_inbox_triage_inflight",
            "owner_id",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running', 'paused', 'recovery_pending')"),
            postgresql_where=text("status IN ('queued', 'running', 'paused', 'recovery_pending')"),
        ),
    )


class InboxTriageRunItem(TimestampMixin, Base):
    """One frozen EmailThread member of an InboxTriageRun snapshot."""

    __tablename__ = "inbox_triage_run_items"
    id = Column(Integer, primary_key=True)
    triage_run_id = Column(Integer, ForeignKey("inbox_triage_runs.id"), nullable=False, index=True)
    email_thread_id = Column(Integer, ForeignKey("email_threads.id"), nullable=False, index=True)
    status = Column(String(20), default="queued", nullable=False, index=True)
    # queued | running | completed | failed | skipped
    outcome = Column(String(30), nullable=True)
    error = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("triage_run_id", "email_thread_id", name="uq_triage_run_thread"),
    )


class Campaign(TimestampMixin, Base):
    __tablename__ = "campaigns"
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    status = Column(String(30), default="draft", nullable=False, index=True)
    # draft | active | paused | stopped
    objective = Column(String(300), nullable=True)
    product_description = Column(Text, nullable=True)
    target_audience = Column(Text, nullable=True)
    sender_name = Column(String(200), nullable=True)
    sender_company = Column(String(200), nullable=True)
    tone = Column(String(50), default="professional", nullable=False)
    primary_agent = Column(String(20), default="langgraph", nullable=False)
    # langgraph | openclaw
    agent_mode = Column(String(20), default="langgraph_only", nullable=False)
    # langgraph_only | openclaw_only | compare
    daily_send_limit = Column(Integer, default=20, nullable=False)
    sending_window_start = Column(Integer, default=9, nullable=False)  # hour 0-23
    sending_window_end = Column(Integer, default=18, nullable=False)
    timezone = Column(String(50), default="Asia/Shanghai", nullable=False)
    max_follow_ups = Column(Integer, default=2, nullable=False)
    follow_up_intervals_days = Column(String(100), default="3,4", nullable=False)
    # comma separated days between follow-ups
    approval_mode = Column(String(20), default="human_confirm", nullable=False)
    # human_confirm | auto  (auto disabled by default)
    confidence_threshold = Column(Integer, default=70, nullable=False)  # 0-100

    owner = relationship("User", back_populates="campaigns")
    contacts = relationship(
        "CampaignContact", back_populates="campaign", cascade="all, delete-orphan"
    )


class Contact(TimestampMixin, Base):
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    email = Column(String(320), nullable=False, index=True)
    first_name = Column(String(150), nullable=True)
    last_name = Column(String(150), nullable=True)
    company = Column(String(200), nullable=True)
    title = Column(String(200), nullable=True)
    phone = Column(String(80), nullable=True)
    website = Column(String(300), nullable=True)
    category = Column(String(40), default="prospect", nullable=False, index=True)
    # prospect | qualified | customer | partner | won | invalid
    tags = Column(Text, nullable=True)  # JSON string array
    intent_level = Column(String(20), default="unknown", nullable=False, index=True)
    # high | medium | low | unknown
    notes = Column(Text, nullable=True)
    lifecycle_stage = Column(String(40), default="new_customer", nullable=False, index=True)
    # new_customer | contacted | awaiting_reply | needs_reply | following_up | won | stopped
    next_action = Column(String(60), nullable=True, index=True)
    # review | send_intro | reply | follow_up | human_review | none
    manual_lock = Column(Boolean, default=False, nullable=False, index=True)
    manual_updated_at = Column(DateTime(timezone=True), nullable=True)
    custom_fields = Column(Text, nullable=True)  # JSON
    status = Column(String(30), default="new", nullable=False, index=True)
    # new | contacted | replied | interested | not_interested | unsubscribed | bounced
    source = Column(String(100), nullable=True)
    last_contacted_at = Column(DateTime(timezone=True), nullable=True)
    next_follow_up_at = Column(DateTime(timezone=True), nullable=True)
    timezone = Column(String(50), nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_id", "email", name="uq_owner_contact_email"),
    )


class CampaignContact(TimestampMixin, Base):
    __tablename__ = "campaign_contacts"
    id = Column(Integer, primary_key=True)
    campaign_id = Column(
        Integer, ForeignKey("campaigns.id"), nullable=False, index=True
    )
    contact_id = Column(
        Integer, ForeignKey("contacts.id"), nullable=False, index=True
    )
    status = Column(String(30), default="queued", nullable=False, index=True)
    # queued | outreach_generated | approved | sent | replied | following_up | done | stopped
    assigned_follow_ups = Column(Integer, default=0, nullable=False)
    last_message_id = Column(String(100), nullable=True)
    thread_id = Column(String(100), nullable=True)
    membership_active = Column(Boolean, default=True, nullable=False, index=True)
    removed_at = Column(DateTime(timezone=True), nullable=True)
    removed_reason = Column(String(120), nullable=True)

    campaign = relationship("Campaign", back_populates="contacts")
    contact = relationship("Contact")

    __table_args__ = (
        UniqueConstraint("campaign_id", "contact_id", name="uq_campaign_contact"),
    )


class EmailThread(TimestampMixin, Base):
    __tablename__ = "email_threads"
    id = Column(Integer, primary_key=True)
    gmail_account_id = Column(
        Integer, ForeignKey("gmail_accounts.id"), nullable=False, index=True
    )
    gmail_thread_id = Column(String(100), nullable=False, index=True)
    gmail_history_id = Column(String(50), nullable=True)
    contact_email = Column(String(320), nullable=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=True, index=True)
    subject = Column(String(500), nullable=True)
    snippet = Column(Text, nullable=True)
    intent = Column(String(30), nullable=True, index=True)
    last_agent_summary = Column(Text, nullable=True)
    pending_action = Column(String(50), nullable=True)
    has_human_reply = Column(Boolean, default=False, nullable=False)

    account = relationship("GmailAccount", back_populates="threads")
    messages = relationship(
        "EmailMessage", back_populates="thread", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "gmail_account_id", "gmail_thread_id", name="uq_account_thread"
        ),
    )


class EmailMessage(TimestampMixin, Base):
    __tablename__ = "email_messages"
    id = Column(Integer, primary_key=True)
    thread_id = Column(
        Integer, ForeignKey("email_threads.id"), nullable=False, index=True
    )
    gmail_message_id = Column(String(100), nullable=True, index=True)
    gmail_history_id = Column(String(50), nullable=True)
    # Gmail's API id is not the RFC Message-ID. These headers are required for
    # a generated reply to remain in the original Gmail conversation.
    message_id_header = Column(String(998), nullable=True)
    in_reply_to_header = Column(String(998), nullable=True)
    references_header = Column(Text, nullable=True)
    from_email = Column(String(320), nullable=True)
    to_email = Column(String(320), nullable=True)
    subject = Column(String(500), nullable=True)
    snippet = Column(Text, nullable=True)
    body_text = Column(Text, nullable=True)
    body_html = Column(Text, nullable=True)
    is_incoming = Column(Boolean, default=True, nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=True)
    # attachment metadata only (JSON): [{filename, mimeType, size, attachmentId}]
    attachments_meta = Column(Text, nullable=True)

    thread = relationship("EmailThread", back_populates="messages")


class EmailDraft(TimestampMixin, Base):
    __tablename__ = "email_drafts"
    id = Column(Integer, primary_key=True)
    gmail_account_id = Column(
        Integer, ForeignKey("gmail_accounts.id"), nullable=False, index=True
    )
    gmail_draft_id = Column(String(100), nullable=True)
    campaign_contact_id = Column(
        Integer, ForeignKey("campaign_contacts.id"), nullable=True, index=True
    )
    thread_id = Column(Integer, ForeignKey("email_threads.id"), nullable=True, index=True)
    to_email = Column(String(320), nullable=False)
    subject = Column(String(500), nullable=False)
    body_text = Column(Text, nullable=False)
    body_html = Column(Text, nullable=True)
    kind = Column(String(20), default="outreach", nullable=False)
    # outreach | follow_up
    agent = Column(String(20), nullable=True)
    idempotency_key = Column(String(120), nullable=True, index=True)
    status = Column(String(20), default="draft", nullable=False)
    # draft | approved | sent | cancelled | failed

    __table_args__ = (
        Index("ix_draft_idem", "idempotency_key", "status"),
    )


class FollowUpTask(TimestampMixin, Base):
    __tablename__ = "follow_up_tasks"
    id = Column(Integer, primary_key=True)
    campaign_contact_id = Column(
        Integer, ForeignKey("campaign_contacts.id"), nullable=False, index=True
    )
    contact_id = Column(
        Integer, ForeignKey("contacts.id"), nullable=False, index=True
    )
    campaign_id = Column(
        Integer, ForeignKey("campaigns.id"), nullable=False, index=True
    )
    thread_id = Column(Integer, ForeignKey("email_threads.id"), nullable=True, index=True)
    sequence = Column(Integer, default=1, nullable=False)  # Nth follow-up
    scheduled_at = Column(DateTime(timezone=True), nullable=False, index=True)
    status = Column(String(20), default="scheduled", nullable=False, index=True)
    # scheduled | ready | running | done | cancelled | paused | failed
    idempotency_key = Column(String(120), nullable=True, index=True)
    last_error = Column(Text, nullable=True)
    agent = Column(String(20), nullable=True)


class Suppression(TimestampMixin, Base):
    __tablename__ = "suppressions"
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    email = Column(String(320), nullable=False, index=True)
    reason = Column(String(30), nullable=False)
    # unsubscribe | not_interested | complaint | bounce | manual
    source = Column(String(50), nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_id", "email", name="uq_owner_suppression_email"),
    )


class NonCustomerFilter(TimestampMixin, Base):
    """Inbox/CRM exclusion; intentionally separate from send Suppression."""
    __tablename__ = "non_customer_filters"
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    email = Column(String(320), nullable=False, index=True)
    reason = Column(String(200), nullable=True)
    source = Column(String(50), default="inbox_review", nullable=False)
    created_by = Column(String(50), default="user", nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "email", name="uq_owner_non_customer_email"),
    )


class InboxReviewRule(TimestampMixin, Base):
    """Durable sender-level handling for explicitly accepted Inbox review rules."""
    __tablename__ = "inbox_review_rules"
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    email = Column(String(320), nullable=False, index=True)
    review_kind = Column(String(60), nullable=False, index=True)
    action = Column(String(60), nullable=False, default="no_action")
    created_by = Column(String(50), default="user", nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "email", "review_kind", name="uq_owner_inbox_review_rule"),
    )


class AgentRun(TimestampMixin, Base):
    __tablename__ = "agent_runs"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(60), nullable=False, unique=True, index=True)
    agent = Column(String(20), nullable=False, index=True)  # langgraph | openclaw
    mode = Column(String(20), nullable=True)  # langgraph_only | openclaw_only | compare
    task_type = Column(String(30), nullable=False, index=True)
    # analyze_message | generate_outreach | generate_follow_up | plan_next_action
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=True, index=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True, index=True)
    thread_id = Column(Integer, ForeignKey("email_threads.id"), nullable=True, index=True)
    intent = Column(String(30), nullable=True, index=True)
    confidence = Column(Integer, nullable=True)
    recommended_action = Column(String(30), nullable=True)
    risk_level = Column(String(10), nullable=True)
    requires_approval = Column(Boolean, nullable=True)
    summary = Column(Text, nullable=True)
    reasoning_summary = Column(Text, nullable=True)
    model = Column(String(80), nullable=True)
    prompt_version = Column(String(30), nullable=True)
    latency_ms = Column(Integer, nullable=True)
    status = Column(String(20), default="success", nullable=False, index=True)
    # success | failed | degraded
    error = Column(Text, nullable=True)
    result_json = Column(Text, nullable=True)  # full structured decision (no hidden CoT)


class AgentComparison(TimestampMixin, Base):
    __tablename__ = "agent_comparisons"
    id = Column(Integer, primary_key=True)
    comparison_key = Column(String(80), nullable=False, index=True)
    # stable key for same input (e.g. thread_id+message)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=True, index=True)
    thread_id = Column(Integer, ForeignKey("email_threads.id"), nullable=True, index=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True, index=True)
    task_type = Column(String(30), nullable=False)
    langgraph_run_id = Column(String(60), nullable=True)
    openclaw_run_id = Column(String(60), nullable=True)
    intent_agree = Column(Boolean, nullable=True)
    action_agree = Column(Boolean, nullable=True)
    selected = Column(String(20), nullable=True)  # langgraph | openclaw | edited | none
    adopted = Column(Boolean, nullable=True)
    notes = Column(Text, nullable=True)


class Approval(TimestampMixin, Base):
    __tablename__ = "approvals"
    id = Column(Integer, primary_key=True)
    kind = Column(String(20), nullable=False, index=True)  # first_send | follow_up
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=True, index=True)
    automation_run_id = Column(Integer, ForeignKey("automation_runs.id"), nullable=True, index=True)
    campaign_contact_id = Column(
        Integer, ForeignKey("campaign_contacts.id"), nullable=True, index=True
    )
    draft_id = Column(Integer, ForeignKey("email_drafts.id"), nullable=True, index=True)
    thread_id = Column(Integer, ForeignKey("email_threads.id"), nullable=True, index=True)
    agent = Column(String(20), nullable=True)
    agent_run_id = Column(String(60), nullable=True, index=True)
    model = Column(String(80), nullable=True)
    prompt_version = Column(String(30), nullable=True)
    latency_ms = Column(Integer, nullable=True)
    quality_json = Column(Text, nullable=True)
    to_email = Column(String(320), nullable=False)
    subject = Column(String(500), nullable=False)
    body_text = Column(Text, nullable=True)
    body_html = Column(Text, nullable=True)
    recommended_action = Column(String(30), nullable=True)
    risk_level = Column(String(10), nullable=True)
    idempotency_key = Column(String(120), nullable=True, index=True)
    status = Column(String(20), default="pending", nullable=False, index=True)
    # pending | approved | rejected | expired
    decided_by = Column(String(200), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    rejection_reason = Column(Text, nullable=True)


class ToolExecution(TimestampMixin, Base):
    __tablename__ = "tool_executions"
    id = Column(Integer, primary_key=True)
    tool_name = Column(String(40), nullable=False, index=True)
    agent = Column(String(20), nullable=True, index=True)
    mode = Column(String(20), nullable=True)
    is_primary = Column(Boolean, nullable=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=True, index=True)
    gmail_account_id = Column(Integer, ForeignKey("gmail_accounts.id"), nullable=True, index=True)
    idempotency_key = Column(String(120), nullable=True, index=True)
    allowed = Column(Boolean, nullable=True)
    blocked_reason = Column(String(120), nullable=True)
    status = Column(String(20), default="ok", nullable=False, index=True)
    # ok | blocked | failed
    latency_ms = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    request_json = Column(Text, nullable=True)
    response_meta = Column(Text, nullable=True)


class DeliveryAttempt(TimestampMixin, Base):
    """Durable Gmail send ledger used for timeout-safe reconciliation."""

    __tablename__ = "delivery_attempts"
    id = Column(Integer, primary_key=True)
    approval_id = Column(Integer, ForeignKey("approvals.id"), nullable=True, index=True)
    draft_id = Column(Integer, ForeignKey("email_drafts.id"), nullable=True, index=True)
    thread_id = Column(Integer, ForeignKey("email_threads.id"), nullable=True, index=True)
    gmail_account_id = Column(Integer, ForeignKey("gmail_accounts.id"), nullable=False, index=True)
    idempotency_key = Column(String(120), nullable=True, unique=True, index=True)
    gmail_draft_id = Column(String(100), nullable=True)
    gmail_message_id = Column(String(100), nullable=True, index=True)
    status = Column(String(30), default="prepared", nullable=False, index=True)
    # prepared | sending | gmail_sent | verified | unknown | failed
    send_requested_at = Column(DateTime(timezone=True), nullable=True)
    gmail_accepted_at = Column(DateTime(timezone=True), nullable=True)
    sync_verified_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)


class AuditLog(TimestampMixin, Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    actor = Column(String(40), nullable=False, index=True)  # user | langgraph | openclaw | system
    action = Column(String(60), nullable=False, index=True)
    entity = Column(String(40), nullable=True)
    entity_id = Column(String(60), nullable=True)
    detail = Column(Text, nullable=True)
    success = Column(Boolean, default=True, nullable=False)


class SystemFlag(TimestampMixin, Base):
    """Key/value runtime flags (e.g. global pause switch)."""
    __tablename__ = "system_flags"
    key = Column(String(60), primary_key=True)
    value = Column(Text, nullable=True)


class Automation(TimestampMixin, Base):
    """A natural-language-driven email automation. OpenClaw (if any) acts ONLY as a
    5-minute cron/webhook trigger that calls POST /api/automation/tick; all Gmail
    reads/writes still go through the existing Policy Engine + tool layer.
    """

    __tablename__ = "automations"
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    prompt = Column(Text, nullable=False)  # original natural-language prompt
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=True, index=True)
    # Null only for the global inbox automation. Campaign automations retain a
    # campaign id and their own cadence/content rules.
    scope = Column(String(20), default="campaign", nullable=False, index=True)
    # global | campaign
    plan_json = Column(Text, nullable=False)  # JSON-encoded AutomationPlan
    status = Column(String(20), default="disabled", nullable=False, index=True)
    # disabled | enabled | paused
    tick_interval_minutes = Column(Integer, default=5, nullable=False)
    next_run_at = Column(DateTime(timezone=True), nullable=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_status = Column(String(20), nullable=True)  # success | partial | failed
    execution_mode = Column(String(20), default="full_auto", nullable=False)
    # full_auto | semi_auto


class AutomationRun(TimestampMixin, Base):
    """One execution of an automation (manual run-now or OpenClaw cron tick)."""

    __tablename__ = "automation_runs"
    id = Column(Integer, primary_key=True)
    automation_id = Column(Integer, ForeignKey("automations.id"), nullable=False, index=True)
    trigger = Column(String(20), nullable=False)  # manual | cron
    source = Column(String(20), nullable=False)  # backend | openclaw
    # queued -> running -> awaiting_confirmation -> confirmed -> running -> success | partial | failed
    status = Column(String(20), default="queued", nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    synced_threads = Column(Integer, default=0, nullable=False)
    approvals_created = Column(Integer, default=0, nullable=False)
    drafts_created = Column(Integer, default=0, nullable=False)
    follow_ups_resolved = Column(Integer, default=0, nullable=False)
    replies_stopped = Column(Integer, default=0, nullable=False)
    summary = Column(Text, nullable=True)
    timeline_json = Column(Text, nullable=True)  # per-contact action list (JSON)
    error = Column(Text, nullable=True)
    execution_mode = Column(String(20), default="full_auto", nullable=False)
    execution_plan_json = Column(Text, nullable=True)
    prepared_at = Column(DateTime(timezone=True), nullable=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    confirmed_by = Column(String(200), nullable=True)
    confirmation_expires_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # DB-layer guarantee: at most ONE active or prepared run per
        # Automation at any moment. Enforced by SQLite's partial unique index,
        # so concurrent/rapid inserts (e.g. double Run-now clicks) cannot both
        # succeed -- the loser gets IntegrityError and is folded into the winner.
        Index(
            "uq_automation_inflight",
            automation_id,
            unique=True,
            sqlite_where=text("status IN ('queued', 'running', 'recovery_pending', 'awaiting_confirmation', 'confirmed')"),
        ),
    )
