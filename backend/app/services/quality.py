"""Deterministic, transparent quality signals for generated outreach.

These are a first-pass, reproducible score for human testers — NOT a substitute
for human judgment. The most important output is the **fabrication** flag: it
detects emails that reference a prior relationship, cooperation history, or
business data that is NOT present in the contact/campaign record. The system
must never fabricate such facts (safety requirement: truthful generation).
"""
from __future__ import annotations

import json
import re

# Phrases that indicate the model invented a prior relationship / data.
_FABRICATION_PATTERNS = [
    r"as (we )?discussed",
    r"as discussed",
    r"our (previous|last|prior) (conversation|call|meeting|chat|email)",
    r"following up on (our |the )?(call|meeting|conversation)",
    r"per our conversation",
    r"as promised",
    r"we spoke",
    r"we talked",
    r"previously mentioned",
    r"you mentioned (that|how|when)",
    r"your (team|company) at ",  # handled further below with the wrong-company check
]


def _norm(s: str) -> str:
    return (s or "").lower()


def compute_quality(subject: str, body_text: str, contact: dict, campaign: dict) -> dict:
    """Return a transparent quality report for one generated email.

    Scores are 1–5. ``fabrication`` is a bool. All inputs come from the real
    contact/campaign records so the scoring cannot invent its own facts.
    """
    text = _norm(f"{subject}\n{body_text}")
    c = contact or {}
    cmp = campaign or {}

    notes: list[str] = []

    # --- Personalization (1-5): uses the recipient's real name / company / title.
    fn = (c.get("first_name") or "").strip()
    comp = (c.get("company") or "").strip()
    title = (c.get("title") or "").strip()
    pscore = 0
    if fn and fn.lower() in text:
        pscore += 2
        notes.append("uses contact first name")
    elif fn:
        notes.append("first name available but not used")
    if comp and comp.lower() in text:
        pscore += 2
        notes.append("uses contact company")
    elif comp:
        notes.append("company available but not used")
    if title and title.lower() in text:
        pscore += 1
        notes.append("uses contact title")
    personalization = max(1, min(5, pscore or 1))

    # --- Business relevance (1-5): references product / objective / audience.
    prod = _norm(cmp.get("product_description") or "")
    obj = _norm(cmp.get("objective") or "")
    aud = _norm(cmp.get("target_audience") or "")
    rscore = 1
    if prod and any(w in text for w in prod.split() if len(w) > 3):
        rscore += 2
    if obj and any(w in text for w in obj.split() if len(w) > 3):
        rscore += 1
    if aud and any(w in text for w in aud.split() if len(w) > 3):
        rscore += 1
    business_relevance = max(1, min(5, rscore))

    # --- CTA (1-5): contains a question or a call-to-action phrase.
    cta_phrases = [
        "would you", "are you open", "could we", "happy to", "let me know",
        "worth a", "open to", "schedule", "a quick call", "a short chat", "?",
    ]
    cta_hits = sum(1 for p in cta_phrases if p in text)
    cta = max(1, min(5, 1 + cta_hits))

    # --- Naturalness (1-5): no markdown, greeting present, sane length.
    nat = 5
    if "**" in body_text or "#" in body_text or "`" in body_text:
        nat -= 2
        notes.append("contains markdown")
    if not re.search(r"(hi|hello|dear|greetings)", text):
        nat -= 1
    words = len(body_text.split())
    if words < 20:
        nat -= 1
    if words > 400:
        nat -= 1
    naturalness = max(1, min(5, nat))

    # --- Realism / fabrication check.
    fab_flags: list[str] = []
    for pat in _FABRICATION_PATTERNS:
        if re.search(pat, text):
            fab_flags.append(pat)
    sender_company = _norm(cmp.get("sender_company") or "")
    if sender_company and re.search(rf"your (team|company) at {re.escape(sender_company)}", text):
        fab_flags.append("attributes recipient company as sender company")
    fabrication = bool(fab_flags)
    realism = 5 if not fabrication else 2

    return {
        "realism": realism,
        "personalization": personalization,
        "naturalness": naturalness,
        "business_relevance": business_relevance,
        "cta": cta,
        "fabrication": fabrication,
        "fabrication_flags": fab_flags,
        "notes": "; ".join(notes),
    }


def quality_to_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False)
