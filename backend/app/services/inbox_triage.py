"""Three-stage Inbox gate: filter noise, verify a human, then derive tags."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import models
from ..config import get_settings, is_llm_configured
from .ai_config import peek_email_config


SYSTEM_LOCAL_PARTS = (
    "no-reply",
    "noreply",
    "do-not-reply",
    "donotreply",
    "mailer-daemon",
    "postmaster",
    "notifications",
)
SYSTEM_SUBJECT_PATTERNS = (
    r"verification code",
    r"security alert",
    r"password reset",
    r"reset your password",
    r"sign[- ]?in",
    r"login",
    r"one[- ]?time pass",
    r"\botp\b",
    r"验证码",
    r"验证邮件",
    r"重设密码",
    r"登录",
)
AD_PATTERNS = (
    r"\bnewsletter\b",
    r"\bweekly digest\b",
    r"\bview in browser\b",
    r"\bmanage preferences\b",
    r"\bpromotional\b",
    r"\blimited time\b",
    r"\bspecial offer\b",
    r"\bhandpicked events\b",
    r"\bunsubscribe\b",
    r"取消订阅",
    r"退订",
    r"活动推荐",
)
SPAM_PATTERNS = (
    r"\bcrypto investment\b",
    r"\bguaranteed return\b",
    r"\bwire transfer\b",
    r"\bclaim your prize\b",
    r"\burgent payment\b",
)


@dataclass
class TriageResult:
    disposition: str  # human | system | advertising | spam | review | outbound
    is_human: bool | None
    confidence: float
    tags: list[str] = field(default_factory=list)
    reason: str = ""
    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None

    @property
    def filtered(self) -> bool:
        return self.disposition in {"system", "advertising", "spam", "outbound"}


# scripted_content means automated/script-generated content, not a Workspace operating mode.
MANUAL_REVIEW_TAGS = frozenset({"job_application", "forwarded", "scripted_content"})


def requires_human_review(tags: list[str]) -> bool:
    return bool(MANUAL_REVIEW_TAGS.intersection(tags))


def clear_stale_human_review(db, thread, *, owner_id: int, actor: str) -> bool:
    """Clear only obsolete Contact-admission reviews; never clear opt-out review."""
    if thread.pending_action != "human_review" or thread.intent in {"unsubscribe", "opt_out"}:
        return False
    email = (thread.contact_email or "").strip().lower()
    contact = (
        db.query(models.Contact).filter_by(owner_id=owner_id, email=email).first()
        if email else None
    )
    latest = max(
        thread.messages,
        key=lambda message: message.received_at or message.created_at,
        default=None,
    )
    latest_outbound = latest is not None and not latest.is_incoming
    if not (contact or latest_outbound):
        return False
    thread.pending_action = "no_action"
    if latest_outbound and not thread.intent:
        thread.intent = "outbound_only"
    db.add(models.AuditLog(
        actor=actor,
        action="stale_human_review_cleared",
        entity="email_thread",
        entity_id=str(thread.id),
        detail=(
            f"reason={'contact_exists' if contact else 'latest_outbound'}; "
            f"contact_id={contact.id if contact else ''}; contact_email={thread.contact_email or ''}"
        ),
        success=True,
    ))
    return True


def sales_reply_required(intent: str | None, tags: list[str]) -> bool:
    """A verified person and a sales-reply task are intentionally distinct."""
    return not requires_human_review(tags) and intent in {
        "interested", "asking_question", "objection"
    }


def _matches(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _content_tags(subject: str, body: str) -> list[str]:
    text = f"{subject}\n{body}".lower()
    tags = []
    rules = {
        "job_application": r"\bresume\b|\bcv\b|\bjob application\b|\bposition\b|求职|应聘|简历",
        "pricing": r"\bprice\b|\bpricing\b|\bcost\b|\bquote\b|报价|价格|费用",
        "demo_request": r"\bdemo\b|\bmeeting\b|\bcall\b|演示|试用|会议",
        "partnership": r"\bpartner(?:ship)?\b|\bcollaborat(?:e|ion)\b|合作",
        "support_request": r"\bsupport\b|\bhelp\b|\bissue\b|\bproblem\b|售后|帮助|问题",
        "complaint": r"\bcomplaint\b|\bdispute\b|\bunhappy\b|投诉|争议|不满",
        "forwarded": r"^\s*(fw|fwd)\s*:|forwarded message|转发邮件",
    }
    for tag, pattern in rules.items():
        if re.search(pattern, text, re.IGNORECASE):
            tags.append(tag)
    if re.search(
        r"\btest(?:ing)?\b|test content|\u6d4b\u8bd5|\u6d4b\u8bd5\u7528|"
        r"\u6d4b\u8bd5\u5185\u5bb9|\u6d4b\u8bd5\u540d\u5355",
        text,
        re.IGNORECASE,
    ):
        tags.append("scripted_content")
    return tags


def _fallback_identity(body: str, tags: list[str]) -> tuple[str | None, str | None, str | None]:
    """Extract explicit sender identity only; forwarded text is not sender identity."""
    if "forwarded" in tags:
        return None, None, None
    tail = (body or "")[-1600:]
    name_match = re.search(
        r"(?:my name is|this is|i am|i'm)\s+([A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+){0,2})",
        tail,
        re.IGNORECASE,
    )
    if not name_match:
        name_match = re.search(
            r"(?:^|\n)(?:best|regards|thanks|sincerely)[,!\s]*\n\s*"
            r"([A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+){0,2})\s*$",
            tail,
            re.IGNORECASE | re.MULTILINE,
        )
    words = name_match.group(1).strip().split() if name_match else []
    company_match = re.search(
        r"(?:^|\n)(?:company|organization|org|from)\s*[:：-]\s*([^\n]{2,100})",
        tail,
        re.IGNORECASE,
    )
    company = company_match.group(1).strip(" .,-") if company_match else None
    return words[0] if words else None, " ".join(words[1:]) or None, company


def _llm_human_assessment(
    from_email: str, subject: str, body: str, db=None
) -> TriageResult | None:
    configured = False
    if db is not None:
        cfg = peek_email_config(db)
        configured = bool(cfg and cfg.api_key and cfg.model)
    if not configured:
        settings = get_settings()
        configured = is_llm_configured(settings)
    if not configured:
        return None
    try:
        from pydantic import BaseModel, Field
        from ..agents.langgraph_agent import _make_llm

        class Assessment(BaseModel):
            disposition: str
            confidence: float = Field(ge=0.0, le=1.0)
            tags: list[str] = []
            reason: str
            first_name: str | None = None
            last_name: str | None = None
            company: str | None = None

        llm = _make_llm(db=db)
        if llm is None:
            return None
        out = llm.with_structured_output(Assessment).invoke(
            "Classify an inbound email before CRM creation. "
            "disposition must be exactly one of human, system, advertising, spam, review. "
            "human means a real person is communicating directly, regardless of whether "
            "they are a sales prospect. advertising includes newsletters, event promotions "
            "and bulk outreach. system includes verification, account and automated notices. "
            "Return short descriptive content tags, not permanent identity categories. "
            "Extract first_name, last_name and company only when explicitly stated by "
            "the current sender in their message or signature; never infer it from forwarded text.\n\n"
            f"From: {from_email}\nSubject: {subject}\nBody:\n{body[:5000]}"
        )
        disposition = out.disposition if out.disposition in {
            "human", "system", "advertising", "spam", "review"
        } else "review"
        is_human = True if disposition == "human" else (
            None if disposition == "review" else False
        )
        tags = sorted(set(out.tags + _content_tags(subject, body)))
        first_name, last_name, company = _fallback_identity(body, tags)
        return TriageResult(
            disposition=disposition,
            is_human=is_human,
            confidence=float(out.confidence),
            tags=tags,
            reason=out.reason[:500],
            first_name=out.first_name or first_name,
            last_name=out.last_name or last_name,
            company=out.company or company,
        )
    except Exception:
        return None


def assess_inbound(
    *,
    is_incoming: bool,
    from_email: str | None,
    subject: str | None,
    body: str | None,
    known_relationship: bool = False,
    db=None,
) -> TriageResult:
    """Apply deterministic filters, then human assessment for remaining mail."""
    if not is_incoming:
        return TriageResult("outbound", False, 1.0, ["outbound_only"], "No inbound message")

    email = (from_email or "").strip().lower()
    subject_text = subject or ""
    body_text = body or ""
    text = f"{subject_text}\n{body_text}"
    local_part = email.split("@", 1)[0] if "@" in email else email

    if any(marker in local_part for marker in SYSTEM_LOCAL_PARTS) or _matches(
        SYSTEM_SUBJECT_PATTERNS, text
    ):
        return TriageResult("system", False, 0.99, ["system"], "Automated sender or system subject")
    if _matches(SPAM_PATTERNS, text):
        return TriageResult("spam", False, 0.95, ["spam"], "Spam pattern detected")
    # A known contact may write "unsubscribe" as a real opt-out. For unknown
    # senders, bulk footer and promotional patterns are filtered before intent.
    if not known_relationship and _matches(AD_PATTERNS, text):
        return TriageResult(
            "advertising", False, 0.92, ["advertising"], "Bulk marketing pattern detected"
        )

    assessed = _llm_human_assessment(email, subject_text, body_text, db=db)
    if assessed:
        return assessed

    # Conservative offline fallback: when the LLM is unavailable (not configured
    # or the call failed) we must NOT assume "definitely human" — that would
    # bypass the human_review gate and auto-create Contacts from unverified mail.
    # Route to human review instead so a person decides before any CRM write.
    tags = _content_tags(subject_text, body_text)
    first_name, last_name, company = _fallback_identity(body_text, tags)
    return TriageResult(
        "review",
        None,
        0.5,
        tags,
        "LLM unavailable; routed to human review",
        first_name,
        last_name,
        company,
    )


def intent_tags(intent: str | None) -> list[str]:
    mapping = {
        "interested": ["interested"],
        "asking_question": ["asking_question"],
        "objection": ["objection"],
        "not_interested": ["not_interested"],
        "unsubscribe": ["unsubscribe"],
        "opt_out": ["opt_out"],
        "bounce": ["bounce"],
        "out_of_office": ["out_of_office"],
    }
    return mapping.get(intent or "", [])


# ---------------------------------------------------------------------------
# Live thread-direction helpers (single source of truth for "needs reply")
# ---------------------------------------------------------------------------
# These mirror the per-thread direction logic the Inbox UI uses, but live in the
# shared service layer so BOTH the API (analyze) and the sync service can derive a
# Contact's reply state from the live direction of its threads without an import
# cycle.  The cached Contact fields (lifecycle_stage / next_action) must always
# agree with this, otherwise the Dashboard "needs reply" count and the Agent's
# "how many customers need a reply" answer drift from reality.
INTENT_CATEGORY = {
    "interested": "valid_customer",
    "asking_question": "needs_reply",
    "objection": "has_interest",
    "not_interested": "not_now",
    "unsubscribe": "rejected",
    "opt_out": "rejected",
    "bounce": "rejected",
    "out_of_office": "irrelevant",
    "unknown": "irrelevant",
    "outbound_only": "awaiting_reply",
    "triage_review": "unsorted",
    "filtered_system": "filtered",
    "filtered_advertising": "filtered",
    "filtered_spam": "filtered",
    "filtered_non_customer": "filtered",
}


def _thread_category(t: "models.EmailThread") -> str:
    """Authoritative per-thread action derived from the LATEST message direction.

    A stale classifier intent must not keep a thread in Needs Reply after we have
    already answered: once the latest message is outbound, the thread is waiting
    for the customer.
    """
    if t.pending_action == "human_review":
        return "human_review"
    latest = max(
        t.messages,
        key=lambda message: message.received_at or message.created_at,
        default=None,
    )
    if latest is not None and not latest.is_incoming:
        return "awaiting_reply"
    return INTENT_CATEGORY.get(t.intent, "irrelevant")


# Reply states recompute must never downgrade (terminal / separately reported).
_PROTECTED_REPLY_STAGES = {"stopped", "won"}
_PROTECTED_NEXT_ACTIONS = {"human_review"}
# A contact that has explicitly opted out / declined / bounced is terminal. Even
# if a LATER inbound message looks like a fresh question, we must NOT auto-flip
# it back to needs_reply -- the original opt-out/decline stands until a human
# explicitly revives the contact. (This is what stopped a false "unsubscribe"
# from being undone by the customer's follow-up reply.)
_PROTECTED_STATUSES = {"unsubscribed", "not_interested", "bounced"}


def contact_needs_reply(db, contact) -> bool:
    """Pure check (no writes): does this Contact have any live thread that is still
    awaiting our reply?  Mirrors _thread_category so the live answer matches the
    Inbox UI.  Terminal/review states are treated as not-needs-reply."""
    if contact is None:
        return False
    if (
        contact.lifecycle_stage in _PROTECTED_REPLY_STAGES
        or contact.next_action in _PROTECTED_NEXT_ACTIONS
        or contact.status in _PROTECTED_STATUSES
    ):
        return False
    for t in db.query(models.EmailThread).filter_by(contact_email=contact.email).all():
        if _thread_category(t) == "needs_reply":
            return True
    return False


def recompute_contact_reply_state(db, contact) -> bool:
    """Re-derive a Contact's reply state from the LIVE direction of ALL its threads.

    Returns True if the cached lifecycle_stage/next_action changed.  This is the
    single source of truth: the per-thread classifier only sets a tentative state,
    and a later outbound reply (pulled in by sync) flips needs_reply -> awaiting_reply
    without waiting for a manual re-analyze.  Protected states are left untouched.
    """
    if contact is None:
        return False
    if (
        contact.lifecycle_stage in _PROTECTED_REPLY_STAGES
        or contact.next_action in _PROTECTED_NEXT_ACTIONS
        or contact.status in _PROTECTED_STATUSES
    ):
        return False
    threads = db.query(models.EmailThread).filter_by(contact_email=contact.email).all()
    needs = any(_thread_category(t) == "needs_reply" for t in threads)
    awaiting = any(_thread_category(t) == "awaiting_reply" for t in threads)
    if needs:
        stage, action = "needs_reply", "reply"
    elif awaiting:
        stage, action = "awaiting_reply", "waiting_for_customer"
    else:
        return False
    if contact.lifecycle_stage == stage and contact.next_action == action:
        return False
    contact.lifecycle_stage = stage
    contact.next_action = action
    db.add(contact)
    return True
