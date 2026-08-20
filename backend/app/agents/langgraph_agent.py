"""LangGraph agent: a real 10-node workflow with conditional branches.

The graph produces a structured AgentDecision. It does NOT call Gmail directly;
any Gmail write is delegated (through the injected UnifiedEmailToolLayer) only
when an explicit execute request + approval is present. By default every send
requires human confirmation (safety rule #1).
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from .. import models
from ..config import get_settings, is_llm_configured
from ..services.ai_config import EmailAIConfig, peek_email_config
from ..knowledge import format_knowledge_context, format_reply_strategy, retrieve_knowledge
from ..services.agent_profile import apply_profile_to_reply, resolve_profile
from ..schemas import (
    AgentDecision,
    AgentHealth,
    DraftPayload,
    EmailProposal,
    AnalyzeMessageInput,
    GenerateOutreachInput,
    GenerateFollowUpInput,
    PlanNextActionInput,
)
from .base import AgentAdapter

logger = logging.getLogger("agent.langgraph")

PROMPT_VERSION = "1.4"

INTENTS = {
    "interested", "asking_question", "objection", "not_interested",
    "unsubscribe", "opt_out", "out_of_office", "bounce", "unknown",
}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
class AgentState(TypedDict, total=False):
    task_type: str
    input: dict
    raw_email: dict
    email: dict
    contact: dict
    campaign: dict
    intent: str
    confidence: float
    policy_ok: bool
    draft: dict
    risk: str
    decision: dict
    error: str
    execute_requested: bool
    approval_id: Optional[int]
    tool_layer: object
    is_primary: bool


# ---------------------------------------------------------------------------
# Heuristic helpers (used when no LLM key, and as deterministic fallback)
# ---------------------------------------------------------------------------
def _opt_out_intent(text: str) -> Optional[tuple[str, float]]:
    """Detect explicit stop requests before asking a model to infer sentiment."""
    t = (text or "").lower()
    if re.search(r"\bunsubscribe\b|退订|取消订阅", t):
        return "unsubscribe", 0.99
    patterns = (
        r"\bopt[ -]?out\b",
        r"\bdo not (?:send|email|contact)\b",
        r"\bdon't (?:send|email|contact)\b",
        r"\bno (?:more|further) (?:emails?|follow[- ]?ups?|contact)\b",
        r"\bstop (?:sending|emailing|contacting|follow[- ]?ups?)\b",
        r"\bplease stop\b",
        r"\bremove me\b",
        r"不要再发|别再发|不要再联系|别再联系|停止跟进",
    )
    if any(re.search(pattern, t) for pattern in patterns):
        return "opt_out", 0.99
    return None


def _heuristic_intent(text: str) -> tuple[str, float]:
    t = (text or "").lower()
    if re.search(r"unsubscribe|退订|取消订阅|remove me", t):
        return "unsubscribe", 0.95
    if re.search(r"not interested|no interest|不感兴趣|暂时不需要|不用了|不需要了", t):
        return "not_interested", 0.9
    if re.search(r"complaint|spam|投诉|垃圾邮件", t):
        return "not_interested", 0.85
    if re.search(r"bounce|mailer-daemon|delivery.*fail|address not found|退回|投递失败", t):
        return "bounce", 0.95
    if re.search(r"out of office|ooo|自动回复|休假|不在办公室|on vacation|假期", t):
        return "out_of_office", 0.92
    # objection: price/value pushback must win over the generic "price" interest rule
    if re.search(r"太高|太贵|贵了|太贵了|负担不起|价格.*(高|贵)|cost.*high|too expensive|价格.*高", t):
        return "objection", 0.75
    # NOTE: no \b word boundaries — CJK text has no word boundaries between
    # characters, so substring matching is required for Chinese keywords.
    if re.search(r"price|pricing|how much|报价|多少钱|费用|方案|demo|试用|interested|感兴趣|yes|好的|可以", t):
        return "interested", 0.7
    if re.search(r"what|how|when|where|why|can you|请问|怎么|如何|什么|能否|是否|哪些|？|\?", t):
        return "asking_question", 0.7
    if re.search(r"\b(but|however|though|但是|不过|可是|担心|顾虑|太贵)\b", t):
        return "objection", 0.6
    return "unknown", 0.4


def _effective_llm_config() -> EmailAIConfig:
    """Resolve the effective LLM config: the DB-backed ``email_ai_config``
    (set via the UI) takes priority, with env/.env as fallback. This mirrors
    the email agent's own resolution so the health badge, the dashboard
    metric, and the actual LLM calls all agree on whether the LLM is
    configured -- regardless of whether the value came from the UI or .env.
    """
    db = getattr(_DB_HOLDER, "db", None)
    if db is not None:
        cfg = peek_email_config(db)
        if cfg and cfg.api_key and cfg.model:
            return cfg
    settings = get_settings()
    return EmailAIConfig(
        provider_name=settings.LLM_PROVIDER or "openai-compatible",
        base_url=settings.LLM_BASE_URL or "https://api.openai.com/v1",
        model=settings.effective_llm_model,
        api_key=settings.effective_llm_api_key or "",
    )


def _make_llm(db=None, *, task: str = "classification"):
    if db is not None:
        cfg = peek_email_config(db)
        if not (cfg and cfg.api_key and cfg.model):
            cfg = _effective_llm_config()
    else:
        cfg = _effective_llm_config()
    if not (cfg.api_key and cfg.model):
        return None
    from langchain_openai import ChatOpenAI

    kwargs = {
        "model": cfg.model,
        "api_key": cfg.api_key,
        "temperature": 0.35 if task in {"reply", "outreach", "follow_up"} else 0,
        "timeout": get_settings().LLM_TIMEOUT_SECONDS,
        "max_retries": 1,
    }
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    if get_settings().LLM_API_MODE == "responses":
        kwargs["use_responses_api"] = True
    return ChatOpenAI(**kwargs)


def _llm_intent(text: str) -> Optional[tuple[str, float]]:
    try:
        from pydantic import BaseModel

        class IntentOut(BaseModel):
            intent: str
            confidence: float

        llm = _make_llm()
        if llm is None:
            return None
        structured = llm.with_structured_output(IntentOut)
        out = structured.invoke(
            f"Classify the email reply intent. Reply ONLY with one of: "
            f"{', '.join(sorted(INTENTS))}.\n\nEmail:\n{text[:4000]}"
        )
        if out.intent in INTENTS:
            return out.intent, float(out.confidence)
    except Exception as e:
        logger.warning("LLM intent failed, falling back to heuristic: %s", e)
    return None


def _llm_draft(contact: dict, campaign: dict, task_type: str, custom_instructions: str = "", last_message_body: str = "") -> Optional[dict]:
    try:
        from pydantic import BaseModel

        class DraftOut(BaseModel):
            subject: str
            body_text: str

        llm = _make_llm(task="follow_up" if task_type == "generate_follow_up" else "outreach")
        if llm is None:
            return None
        owner_id = campaign.get("owner_id") or contact.get("owner_id")
        profile = resolve_profile(
            getattr(_DB_HOLDER, "db", None), owner_id, campaign=campaign
        )
        knowledge_context = format_knowledge_context(
            f"{campaign.get('product_description') or ''}\n{custom_instructions}\n{last_message_body}",
            db=getattr(_DB_HOLDER, "db", None),
            owner_id=owner_id,
        )
        prompt = (
            "Write a concise, truthful B2B outreach email. Use ONLY the facts supplied in the JSON below.\n"
            "Rules:\n"
            "- Never invent the recipient's background, prior conversations, cooperation history, "
            "or business metrics. If a field is missing, use a conservative generic phrasing instead of guessing.\n"
            "- Address the recipient by their first name. Use their company and title if provided and truthful.\n"
            "- Reference SPECIFIC, CONCRETE service points drawn from the Approved knowledge base below "
            "(for example: AI-generated outreach emails, intelligent inbox triage, automated follow-up, "
            "unsubscribe/stop rules). Do not be vague; if the knowledge base is empty, keep claims general "
            "and honest rather than inventing features.\n"
            "- If any knowledge base fragment is unreadable or garbled, ignore it; never reproduce garbled text.\n"
            "- Match the requested tone. Reference the product, objective, and target audience realistically.\n"
            "- No markdown. Return a subject line and a plain-text body only.\n"
            "- Use the Agent Profile below as a mandatory behavior contract. Do not write a closing or "
            "signature; the application appends the exact Profile signature after generation.\n\n"
            "Agent Profile:\n"
            f"Name: {profile['agent_name']}\n"
            f"Company: {profile['company_name']}\n"
            f"Role: {profile['role']}\n"
            f"Tone: {profile['tone']}\n"
            f"Language policy: {profile['language_policy']}\n"
            f"Required signature:\n{profile['signature_text']}\n"
            f"Forbidden claims: {profile['forbidden_claims']}\n"
            f"Unknown-answer policy: {profile['unknown_answer_policy']}\n\n"
            "Approved knowledge base (use only when relevant; never mention the internal knowledge base):\n"
            f"{knowledge_context}\n\n"
            f"Task: {task_type}\n"
            f"Contact: {contact}\n"
            f"Campaign: {campaign}\n"
            f"Custom instructions: {custom_instructions or 'None'}\n"
            f"Previous message: {last_message_body or 'None'}"
        )
        out = llm.with_structured_output(DraftOut).invoke(prompt)
        if out.subject.strip() and out.body_text.strip():
            return {"subject": out.subject.strip(), "body_text": out.body_text.strip(), "body_html": ""}
    except Exception as e:
        logger.warning("LLM draft generation failed, falling back to conservative template: %s", e)
    return None


# ---------------------------------------------------------------------------
# LangGraph nodes
# ---------------------------------------------------------------------------
def load_context(state: AgentState) -> dict:
    return {"policy_ok": True}


def normalize_email(state: AgentState) -> dict:
    inp = state.get("input", {})
    raw = state.get("raw_email") or {}
    email = {
        "subject": raw.get("subject") or inp.get("subject") or "",
        "body_text": raw.get("body_text") or inp.get("body_text") or "",
        "from_email": raw.get("from_email") or inp.get("from_email") or "",
        "thread_context": inp.get("thread_context") or "",
    }
    return {"email": email}


def _strip_quoted_text(text: str) -> str:
    """Drop quoted / forwarded content before intent detection.

    Gmail (and most clients) embed the original message when the customer
    replies. That quoted text can carry keywords such as "unsubscribe" -- e.g.
    inside our own outreach's product copy: "unsubscribe/bounce suppression
    management". Scanning the full reply therefore produced false opt-out /
    unsubscribe hits. We keep only the recipient's own words:
      * lines beginning with '>' or '|' (standard quote markers);
      * everything after an "On <date> <person> wrote:" or
        "-----Original Message-----" header until a blank line ends the block.
    """
    if not text:
        return ""
    out: list[str] = []
    in_quote_block = False
    for line in text.splitlines():
        stripped = line.lstrip()
        low = stripped.lower()
        # Start of a forwarded / "On ... wrote:" quote block.
        if re.match(r"^on\s+.+\s+wrote:\s*$", low) or low.startswith("-----original message-----"):
            in_quote_block = True
            continue
        if in_quote_block:
            # Stay inside the quote until a blank line ends the block.
            if stripped == "":
                in_quote_block = False
            continue
        # Quoted reply lines.
        if stripped.startswith(">") or stripped.startswith("|"):
            continue
        out.append(line)
    return "\n".join(out)


def classify_intent(state: AgentState) -> dict:
    raw = (state.get("email", {}).get("body_text") or "") + " " + (state.get("email", {}).get("subject") or "")
    # Strip quoted/forwarded text FIRST so intent detection never matches words
    # that belong to a previous message the customer is merely replying to.
    text = _strip_quoted_text(raw)
    result = _opt_out_intent(text) or _llm_intent(text)
    if result is None:
        intent, conf = _heuristic_intent(text)
    else:
        intent, conf = result
    return {"intent": intent, "confidence": conf}


def retrieve_contact_and_campaign(state: AgentState) -> dict:
    db = _db()
    inp = state.get("input", {})
    contact = None
    campaign = None
    if inp.get("contact_id"):
        c = db.get(models.Contact, inp["contact_id"])
        if c:
            contact = {k: getattr(c, k) for k in ("id", "owner_id", "email", "first_name", "last_name", "company", "title", "custom_fields")}
    if inp.get("campaign_id"):
        cmp = db.get(models.Campaign, inp["campaign_id"])
        if cmp:
            campaign = {k: getattr(cmp, k) for k in ("id", "owner_id", "name", "product_description", "target_audience", "sender_name", "sender_company", "tone", "objective", "max_follow_ups", "confidence_threshold", "timezone")}
    return {"contact": contact, "campaign": campaign}


def check_reply_and_followup_policy(state: AgentState) -> dict:
    # Decide recommended action based on intent
    intent = state.get("intent", "unknown")
    task_type = state.get("task_type")
    policy_ok = True
    if intent in ("unsubscribe", "opt_out", "not_interested"):
        policy_ok = False  # stop
    return {"policy_ok": policy_ok}


def generate_draft(state: AgentState) -> dict:
    task_type = state.get("task_type")
    intent = state.get("intent", "unknown")
    contact = state.get("contact") or {}
    campaign = state.get("campaign") or {}
    # Only generate a reply draft for incoming replies that warrant one.
    if task_type == "analyze_message":
        if intent in ("interested", "asking_question"):
            email = state.get("email") or {}
            subject, body = _compose_reply(
                contact,
                campaign,
                intent,
                original_subject=email.get("subject") or "",
                customer_message=email.get("body_text") or "",
                thread_context=email.get("thread_context") or "",
            )
            return {"draft": {"subject": subject, "body_text": body, "body_html": ""}}
        return {"draft": None}
    # generate_outreach / generate_follow_up
    inp = state.get("input", {})
    generated = _llm_draft(
        contact,
        campaign,
        task_type,
        custom_instructions=inp.get("custom_instructions") or "",
        last_message_body=inp.get("last_message_body") or "",
    )
    if generated:
        owner_id = campaign.get("owner_id") or contact.get("owner_id")
        profile = resolve_profile(
            getattr(_DB_HOLDER, "db", None), owner_id, campaign=campaign
        )
        cleaned, flags = apply_profile_to_reply(
            generated["body_text"],
            profile,
            customer_name=(contact.get("first_name") or "").strip(),
        )
        if flags:
            logger.info("Agent Profile outbound cleanup: %s", ",".join(flags))
        return {"draft": {"subject": generated["subject"], "body_text": cleaned, "body_html": ""}}
    subject, body = _compose_outreach(contact, campaign, task_type)
    return {"draft": {"subject": subject, "body_text": body, "body_html": ""}}


def _compose_outreach(contact, campaign, task_type):
    first = (contact.get("first_name") or "").strip() or "there"
    owner_id = campaign.get("owner_id") or contact.get("owner_id")
    profile = resolve_profile(getattr(_DB_HOLDER, "db", None), owner_id, campaign=campaign)
    sender_name = profile.get("agent_name") or "Sendy"
    sender_company = profile.get("company_name")
    product = campaign.get("product_description") or "our product"
    audience = campaign.get("target_audience") or "your team"
    objective = campaign.get("objective")
    # Conservative: when the recipient's company is unknown, refer to "your team"
    # rather than borrowing the sender's own company (that would be a fabrication).
    recipient_ref = (contact.get("company") or "").strip() or "your team"
    sig = (profile.get("signature_text") or f"Best regards,\n{sender_name}").strip()
    if task_type == "generate_follow_up":
        subject = f"Following up: {product[:40]}"
        body = (
            f"Hi {first},\n\n"
            f"I just wanted to follow up on my earlier note about {product}. "
            f"We help {audience} and I thought it might be relevant to {recipient_ref}.\n\n"
            f"Were you able to take a look? Happy to answer any questions.\n\n"
            f"{sig}"
        )
    else:
        obj_line = f"{objective}\n\n" if objective else ""
        subject = f"{first}, a quick idea for {recipient_ref}"
        body = (
            f"Hi {first},\n\n"
            f"I'm {sender_name}{(' from ' + sender_company) if sender_company else ''}. {product}\n\n"
            f"{obj_line}"
            f"We work with {audience}, and I thought this might be a good fit for {recipient_ref}.\n\n"
            f"Would you be open to a short chat this week?\n\n"
            f"{sig}"
        )
    return subject, body


def _reply_subject(original_subject: str) -> str:
    subject = (original_subject or "").strip()
    if not subject:
        return "Re: your message"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def _llm_reply(contact, campaign, customer_message: str, thread_context: str) -> Optional[str]:
    try:
        from pydantic import BaseModel

        class ReplyOut(BaseModel):
            body_text: str

        llm = _make_llm(task="reply")
        if llm is None:
            return None
        owner_id = campaign.get("owner_id") or contact.get("owner_id")
        profile = resolve_profile(
            getattr(_DB_HOLDER, "db", None), owner_id, campaign=campaign
        )
        knowledge_context = format_knowledge_context(
            f"{customer_message}\n{thread_context}\n{campaign.get('product_description') or ''}",
            db=getattr(_DB_HOLDER, "db", None),
            owner_id=owner_id,
        )
        reply_strategy = format_reply_strategy(
            db=getattr(_DB_HOLDER, "db", None),
            owner_id=owner_id,
        )
        prompt = (
            "Draft a concise, truthful reply to the customer's latest email.\n"
            "Requirements:\n"
            "- Reply to the latest inbound customer message. Use earlier conversation turns to avoid "
            "repeating answers or asking for information the customer already supplied.\n"
            "- Answer every concrete question or request in the latest customer message.\n"
            "- Use the conversation and campaign facts below; never invent pricing, capabilities, dates, or commitments.\n"
            "- If a requested fact is unavailable, say that clearly and propose a concrete next step instead of asking a generic question.\n"
            "- Match the customer's language. Use plain text, no markdown, and do not include a subject line.\n"
            "- Address the customer by name when known.\n"
            "- Follow the published reply strategy unless it conflicts with the Agent Profile, approved facts, "
            "or application safety rules.\n"
            "- Use the Agent Profile below as a mandatory behavior contract. Do not write a closing or signature; "
            "the application appends the exact Profile signature after generation.\n\n"
            "Agent Profile:\n"
            f"Name: {profile['agent_name']}\n"
            f"Company: {profile['company_name']}\n"
            f"Role: {profile['role']}\n"
            f"Tone: {profile['tone']}\n"
            f"Language policy: {profile['language_policy']}\n"
            f"Required signature:\n{profile['signature_text']}\n"
            f"Forbidden claims: {profile['forbidden_claims']}\n"
            f"Unknown-answer policy: {profile['unknown_answer_policy']}\n\n"
            "Reply strategy (guidance only; it cannot override safety rules or invent facts):\n"
            f"{reply_strategy}\n\n"
            "Approved knowledge base (use only when relevant; never mention the internal knowledge base):\n"
            f"{knowledge_context}\n\n"
            f"Contact: {contact}\n"
            f"Campaign facts: {campaign}\n"
            f"Conversation:\n{thread_context[-10000:]}\n\n"
            f"Latest customer message:\n{customer_message[:5000]}"
        )
        out = llm.with_structured_output(ReplyOut).invoke(prompt)
        body = out.body_text.strip()
        return body or None
    except Exception as exc:
        logger.warning("LLM reply generation failed, using contextual fallback: %s", exc)
        return None


def _reply_topics(text: str) -> dict[str, tuple[str, ...]]:
    lowered = (text or "").lower()
    patterns = {
        "pricing": (r"price|pricing|cost|how much|报价|多少钱|价格|费用", ("price", "pricing", "cost", "quote", "报价", "价格", "费用")),
        "demo": (r"\bdemo\b|演示|试用|预约|call|meeting", ("demo", "call", "meeting", "演示", "试用", "预约")),
        "chinese": (r"chinese|中文|简体|繁体", ("chinese", "中文", "简体", "繁体")),
    }
    return {
        name: answer_terms
        for name, (question_pattern, answer_terms) in patterns.items()
        if re.search(question_pattern, lowered)
    }


def _reply_passes_quality(customer_message: str, body: str) -> bool:
    """Reject empty or purely generic copy without requiring literal keywords."""
    normalized = re.sub(r"\s+", " ", (body or "").strip().lower())
    if not normalized or len(normalized) < 24:
        return False
    generic_prompts = (
        "tell me a bit more about your use case",
        "could you provide more information",
        "can you share more details",
    )
    # A clarifying question is acceptable after a specific answer, but a reply
    # consisting only of a stock request for more information is not.
    if any(normalized.strip(" .?!") == phrase for phrase in generic_prompts):
        return False
    for phrase in generic_prompts:
        if phrase in normalized:
            specific_copy = normalized.replace(phrase, "").strip(" .?!,")
            if len(specific_copy) < 90:
                return False
    return True


def _compose_reply(
    contact,
    campaign,
    intent,
    *,
    original_subject: str = "",
    customer_message: str = "",
    thread_context: str = "",
):
    first = (contact.get("first_name") or "").strip() or "there"
    owner_id = campaign.get("owner_id") or contact.get("owner_id")
    profile = resolve_profile(
        getattr(_DB_HOLDER, "db", None), owner_id, campaign=campaign
    )
    subject = _reply_subject(original_subject)
    generated = _llm_reply(contact, campaign, customer_message, thread_context)
    if generated and _reply_passes_quality(customer_message, generated):
        cleaned, flags = apply_profile_to_reply(
            generated,
            profile,
            customer_message=customer_message,
            customer_name=first if first != "there" else "",
        )
        if flags:
            logger.info("Agent Profile reply cleanup: %s", ",".join(flags))
        return subject, cleaned
    if generated:
        logger.warning("LLM reply failed contextual quality gate; using safe fallback")

    text = (customer_message or "").lower()
    product = campaign.get("product_description") or "our email automation service"
    points = []
    owner_id = campaign.get("owner_id") or contact.get("owner_id")
    knowledge_entries = retrieve_knowledge(
        f"{customer_message}\n{thread_context}",
        db=getattr(_DB_HOLDER, "db", None),
        owner_id=owner_id,
    )
    if knowledge_entries and not re.search(
        r"price|pricing|cost|how much|鎶ヤ环|澶氬皯閽眧浠锋牸|璐圭敤|contract|delivery|quote|价格|报价|合同|交付",
        text,
    ):
        points.append(knowledge_entries[0]["content"])
    if re.search(r"price|pricing|cost|how much|报价|多少钱|价格|费用", text):
        points.append(
            "Pricing depends on the mailbox volume and workflow you need, so I do not want to quote an inaccurate figure by email. "
            "We can confirm the scope and provide an exact quote during the demo."
        )
    if re.search(r"\bdemo\b|演示|试用|预约|call|meeting", text):
        points.append("We would be happy to arrange a short product demo and walk through the workflow with you.")
    if re.search(r"chinese|中文|简体|繁体", text):
        points.append(
            "I will confirm the exact Chinese-language requirements with you during the demo so we only promise what the current setup supports."
        )
    if not points:
        points.append(
            f"Thanks for your message about {product}. I have noted your request and can address the details in a short call."
        )
    body = (
        f"Hi {first},\n\n"
        + "\n\n".join(points)
    )
    cleaned, flags = apply_profile_to_reply(
        body,
        profile,
        customer_message=customer_message,
        customer_name=first if first != "there" else "",
    )
    if flags:
        logger.info("Agent Profile fallback cleanup: %s", ",".join(flags))
    return subject, cleaned


def risk_review(state: AgentState) -> dict:
    intent = state.get("intent", "unknown")
    risk = "low"
    if intent in ("unsubscribe", "opt_out", "not_interested", "bounce"):
        risk = "high"
    elif intent in ("objection", "unknown"):
        risk = "medium"
    return {"risk": risk}


def request_approval_or_execute(state: AgentState) -> dict:
    intent = state.get("intent", "unknown")
    risk = state.get("risk", "low")
    confidence = state.get("confidence", 0.0)
    campaign = state.get("campaign") or {}
    threshold = (campaign.get("confidence_threshold") or 70) / 100.0
    task_type = state.get("task_type")

    # Default: always require human confirmation before any send (safety rule #1)
    requires_approval = True

    if intent in ("unsubscribe", "opt_out"):
        action = "stop"
    elif intent == "not_interested":
        action = "stop"
    elif intent == "bounce":
        action = "stop"
    elif intent == "out_of_office":
        action = "follow_up_later"
    elif intent == "interested":
        action = "human_review"
    elif intent == "asking_question":
        action = "human_review"
    elif intent == "objection":
        action = "human_review"
    else:
        action = "human_review"

    if risk == "high":
        # high risk => never auto execute
        action = "stop" if intent in ("unsubscribe", "opt_out", "not_interested", "bounce") else "human_review"

    decision = {
        "intent": intent,
        "recommended_action": action,
        "risk_level": risk,
        "requires_approval": requires_approval,
        "confidence": confidence,
        "reasoning_summary": _reasoning(intent, risk, action, confidence, threshold),
        "draft": state.get("draft"),
    }
    # Explicit execution path (only when allowed + approved). Kept for completeness;
    # default demo flows never set execute_requested.
    if state.get("execute_requested") and state.get("approval_id") and state.get("is_primary"):
        tl = state.get("tool_layer")
        if tl and state.get("draft"):
            try:
                tl.create_draft(
                    to=state["email"].get("from_email", ""),
                    subject=state["draft"]["subject"],
                    body_text=state["draft"]["body_text"],
                    agent="langgraph",
                    is_primary=True,
                )
            except Exception as e:
                logger.warning("langgraph execute skipped: %s", e)
    return {"decision": decision}


def _reasoning(intent, risk, action, confidence, threshold):
    if intent in ("unsubscribe", "opt_out"):
        return "Recipient requested no further outreach; must stop and add to suppression list."
    if intent == "not_interested":
        return "Recipient expressed no interest; stopping automatic follow-up per policy."
    if intent == "bounce":
        return "Hard bounce detected; marking address undeliverable."
    if intent == "out_of_office":
        return "Auto-reply detected; reschedule follow-up by return date."
    if confidence < threshold:
        return f"Confidence {int(confidence*100)}% below threshold {int(threshold*100)}%; routed to human review."
    if risk == "high":
        return "High risk intent; automatic execution disabled."
    return f"Intent={intent}, risk={risk}; recommended action={action} (human confirmation required)."


def persist_result(state: AgentState) -> dict:
    return {}


def schedule_next_action(state: AgentState) -> dict:
    intent = state.get("intent", "unknown")
    if intent == "out_of_office":
        # would reschedule; handled by scheduler service
        pass
    return {}


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------
def build_graph():
    g = StateGraph(AgentState)
    g.add_node("load_context", load_context)
    g.add_node("normalize_email", normalize_email)
    g.add_node("classify_intent", classify_intent)
    g.add_node("retrieve_contact_and_campaign", retrieve_contact_and_campaign)
    g.add_node("check_reply_and_followup_policy", check_reply_and_followup_policy)
    g.add_node("generate_draft", generate_draft)
    g.add_node("risk_review", risk_review)
    g.add_node("request_approval_or_execute", request_approval_or_execute)
    g.add_node("persist_result", persist_result)
    g.add_node("schedule_next_action", schedule_next_action)

    g.add_edge(START, "load_context")
    g.add_edge("load_context", "normalize_email")
    g.add_edge("normalize_email", "classify_intent")
    g.add_edge("classify_intent", "retrieve_contact_and_campaign")
    g.add_edge("retrieve_contact_and_campaign", "check_reply_and_followup_policy")
    g.add_edge("check_reply_and_followup_policy", "generate_draft")
    g.add_edge("generate_draft", "risk_review")
    g.add_edge("risk_review", "request_approval_or_execute")
    g.add_edge("request_approval_or_execute", "persist_result")
    g.add_edge("persist_result", "schedule_next_action")
    g.add_edge("schedule_next_action", END)
    return g.compile()


# Thread-local db handle set by the adapter instance
import threading
_DB_HOLDER = threading.local()


def _db():
    return _DB_HOLDER.db


class LangGraphAdapter(AgentAdapter):
    agent_name = "langgraph"

    def __init__(self, db=None, tool_layer=None):
        self._db = db
        self._tool_layer = tool_layer
        self._graph = build_graph()

    def _run(self, task_type, inp: dict, raw_email=None, execute_requested=False, approval_id=None, is_primary=True) -> AgentDecision:
        t0 = time.time()
        if self._db is not None:
            _DB_HOLDER.db = self._db
        initial: AgentState = {
            "task_type": task_type,
            "input": inp,
            "raw_email": raw_email or {},
            "execute_requested": execute_requested,
            "approval_id": approval_id,
            "tool_layer": self._tool_layer,
            "is_primary": is_primary,
        }
        result = self._graph.invoke(initial)
        decision = result.get("decision") or {}
        cfg = _effective_llm_config()
        model = cfg.model if (cfg.api_key and cfg.model) else "rule-based"
        return AgentDecision(
            agent="langgraph",
            run_id="run_" + _rand(),
            intent=decision.get("intent", "unknown"),
            confidence=decision.get("confidence", 0.0),
            summary=_summary(decision),
            recommended_action=decision.get("recommended_action", "human_review"),
            reasoning_summary=decision.get("reasoning_summary", ""),
            risk_level=decision.get("risk_level", "low"),
            requires_approval=decision.get("requires_approval", True),
            suggested_follow_up_at=None,
            draft=DraftPayload(**decision["draft"]) if decision.get("draft") else None,
            latency_ms=int((time.time() - t0) * 1000),
            model=model,
            prompt_version=PROMPT_VERSION,
        )

    def analyze_message(self, inp: AnalyzeMessageInput) -> Optional[AgentDecision]:
        raw = None
        if inp.body_text or inp.subject:
            raw = {"subject": inp.subject, "body_text": inp.body_text, "from_email": inp.from_email}
        return self._run("analyze_message", inp.model_dump(), raw_email=raw)

    def generate_outreach(self, inp: GenerateOutreachInput) -> Optional[EmailProposal]:
        d = self._run("generate_outreach", inp.model_dump())
        return _to_proposal(d)

    def generate_follow_up(self, inp: GenerateFollowUpInput) -> Optional[EmailProposal]:
        d = self._run("generate_follow_up", inp.model_dump())
        return _to_proposal(d)

    def plan_next_action(self, inp: PlanNextActionInput) -> Optional[AgentDecision]:
        return self._run("plan_next_action", inp.model_dump())

    def health_check(self) -> AgentHealth:
        if self._db is not None:
            cfg = peek_email_config(self._db)
            configured = bool(cfg and cfg.api_key and cfg.model)
        else:
            configured = is_llm_configured(get_settings())
        return AgentHealth(
            agent="langgraph",
            configured=configured,
            reachable=True,
            mode=None,
            detail="LLM-backed" if configured else "rule-based fallback (no LLM API key)",
        )


def _rand() -> str:
    import secrets
    return secrets.token_hex(12)


def _summary(decision) -> str:
    intent = decision.get("intent", "unknown")
    action = decision.get("recommended_action", "human_review")
    return f"Classified as {intent}; recommended {action}."


def _to_proposal(d: AgentDecision) -> EmailProposal:
    return EmailProposal(
        agent=d.agent,
        run_id=d.run_id,
        subject=d.draft.subject if d.draft else "",
        body_text=d.draft.body_text if d.draft else "",
        body_html=d.draft.body_html if d.draft else "",
        intent=d.intent,
        confidence=d.confidence,
        summary=d.summary,
        recommended_action=d.recommended_action,
        reasoning_summary=d.reasoning_summary,
        risk_level=d.risk_level,
        requires_approval=d.requires_approval,
        latency_ms=d.latency_ms,
        model=d.model,
        prompt_version=d.prompt_version,
    )
