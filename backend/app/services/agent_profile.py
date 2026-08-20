"""Structured Agent identity, inheritance, and deterministic reply cleanup."""
from __future__ import annotations

import re

from .. import models


DEFAULT_PROFILE = {
    "agent_name": "Sendy",
    "company_name": "TAC AISolution",
    "role": "Enterprise AI Solutions Sales Consultant",
    "tone": "professional, consultative, concise, friendly",
    "language_policy": "match_customer",
    "signature_text": "Best regards,\nSendy\nTAC AISolution",
    "forbidden_claims": (
        "pricing,delivery dates,guaranteed results,unapproved customer cases,"
        "compliance guarantees"
    ),
    "unknown_answer_policy": (
        "State that the detail needs confirmation and propose a concrete next step."
    ),
    "allow_campaign_override": False,
    "approval_mode": "human_review",
    "is_active": True,
    "version": 1,
}


def get_or_create_profile(db, owner_id: int) -> models.AgentProfile:
    profile = db.query(models.AgentProfile).filter_by(owner_id=owner_id).first()
    if profile:
        return profile
    profile = models.AgentProfile(owner_id=owner_id, **DEFAULT_PROFILE)
    db.add(profile)
    db.flush()
    db.add(models.AuditLog(
        actor="system",
        action="agent_profile_created",
        entity="agent_profile",
        entity_id=str(profile.id),
        detail="Created default Sendy / TAC AISolution profile.",
    ))
    return profile


def resolve_profile(db, owner_id: int | None, campaign=None) -> dict:
    data = dict(DEFAULT_PROFILE)
    if db is not None and owner_id is not None:
        row = get_or_create_profile(db, owner_id)
        data.update({
            key: getattr(row, key)
            for key in DEFAULT_PROFILE
            if hasattr(row, key)
        })
        data["id"] = row.id
    if campaign and data.get("allow_campaign_override"):
        sender_name = campaign.get("sender_name") if isinstance(campaign, dict) else campaign.sender_name
        sender_company = campaign.get("sender_company") if isinstance(campaign, dict) else campaign.sender_company
        tone = campaign.get("tone") if isinstance(campaign, dict) else campaign.tone
        if _safe_identity_value(sender_name):
            data["agent_name"] = sender_name
        if _safe_identity_value(sender_company):
            data["company_name"] = sender_company
        if tone:
            data["tone"] = tone
        data["signature_text"] = (
            f"Best regards,\n{data['agent_name']}\n{data['company_name']}"
        )
    return data


def _safe_identity_value(value) -> bool:
    text = str(value or "").strip()
    if len(text) < 2 or text.isdigit():
        return False
    return not bool(re.search(
        r"(?i)^(?:1|test|your name|\[name\]|ai team|tac sales|unknown|n/?a)$",
        text,
    ))


_SIGNOFF_LINE = re.compile(
    r"(?im)^\s*(?:best(?:\s+regards)?|regards|kind regards|sincerely|"
    r"thanks|thank you|顺颂商祺|此致|敬礼|祝好|谨致问候)[,，:：!！\s]*(?:.*)?$"
)
_BAD_SIGNATURE = re.compile(
    r"(?i)(?:best\s*,?\s*1|顺颂商祺\s*[，,]\s*1|\[name\]|your name|ai team|tac sales)"
)
_TRAILING_SIGNOFF = re.compile(
    r"(?is)\s*(?:best(?:\s+regards)?|regards|kind regards|sincerely|"
    r"thanks|thank you|顺颂商祺|此致|敬礼|祝好|谨致问候)"
    r"[\s,，:：!！]*(?:1|\[name\]|your name|ai team|tac sales)?\s*$"
)


def apply_profile_to_reply(
    body: str,
    profile: dict,
    *,
    customer_message: str = "",
    customer_name: str = "",
) -> tuple[str, list[str]]:
    """Return a deterministic identity-safe body and quality flags."""
    text = (body or "").strip()
    flags: list[str] = []
    if _BAD_SIGNATURE.search(text):
        flags.append("invalid_model_signature_removed")
    cleaned_inline = _TRAILING_SIGNOFF.sub("", text).rstrip(" \n,，")
    if cleaned_inline != text:
        text = cleaned_inline
        if "model_signoff_removed" not in flags:
            flags.append("model_signoff_removed")

    lines = text.splitlines()
    cut = None
    for index in range(max(0, len(lines) - 6), len(lines)):
        if _SIGNOFF_LINE.match(lines[index]):
            cut = index
            break
    if cut is not None:
        lines = lines[:cut]
    text = "\n".join(lines).rstrip(" \n,，")

    is_chinese = bool(re.search(r"[\u4e00-\u9fff]", customer_message or ""))
    if profile.get("language_policy") == "match_customer" and is_chinese:
        match = re.match(r"(?i)^Hi\s+([^,\n]+),\s*", text)
        if match:
            name = (customer_name or match.group(1)).strip()
            text = f"{name}，您好：\n\n" + text[match.end():].lstrip()
            flags.append("greeting_matched_customer_language")

    signature = (profile.get("signature_text") or "").strip()
    if not signature:
        signature = (
            f"Best regards,\n{profile.get('agent_name') or 'Sendy'}\n"
            f"{profile.get('company_name') or 'TAC AISolution'}"
        )
        flags.append("default_signature_applied")
    final = f"{text}\n\n{signature}".strip()

    if profile.get("agent_name") not in final or profile.get("company_name") not in final:
        raise ValueError("agent_profile_signature_validation_failed")
    if _BAD_SIGNATURE.search(final):
        raise ValueError("unsafe_signature_survived_postprocessing")
    return final, flags
