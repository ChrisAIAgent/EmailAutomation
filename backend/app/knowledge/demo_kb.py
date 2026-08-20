"""Small, auditable built-in local knowledge base.

This source-based implementation intentionally avoids a vector database so the
workspace can run standalone. The entries can later be replaced by
customer-managed document chunks without changing the LangGraph retrieval
contract.
"""
from __future__ import annotations

import re
from typing import Any


DEFAULT_REPLY_STRATEGY = (
    "Reply to the latest customer request while using the conversation to avoid repetition. "
    "Answer confirmed points directly, state unknown details honestly, and propose one clear "
    "next step. Keep the reply concise, natural, and in the customer's language."
)

_CONCEPT_ALIASES: tuple[tuple[str, ...], ...] = (
    ("销售邮件", "邮件自动化", "邮箱自动化", "邮件处理", "邮件回复", "email automation"),
    ("客户跟进", "自动跟进", "销售跟进", "跟进任务", "follow-up", "follow up"),
    ("客户意图", "意图识别", "客户分类", "邮件分拣"),
    ("企业知识库", "知识库", "rag", "检索增强"),
    ("流程自动化", "工作流", "agent 工作流", "流程 agent"),
)


DEMO_KNOWLEDGE: tuple[dict[str, Any], ...] = (
    {
        "id": "product_overview",
        "title": "TAC Email Automation overview",
        "keywords": ("email", "automation", "gmail", "inbox", "follow-up", "邮件", "自动化"),
        "content": (
            "TAC Email Automation helps teams synchronize Gmail, classify inbound conversations, "
            "manage contacts and campaigns, and prepare context-aware replies and follow-ups. "
            "Sending safeguards, approvals, stop rules, and audit records remain in the workflow."
        ),
    },
    {
        "id": "inbox_reply",
        "title": "Inbox reply workflow",
        "keywords": ("reply", "response", "inbox", "thread", "customer", "回复", "收件箱"),
        "content": (
            "Replies use the latest customer message and the surrounding Gmail thread. "
            "The Agent checks whether the sender is a verified human business contact before preparing a sales reply. "
            "A reply stays in the normal Approval or full-auto policy path and is sent in the original Gmail thread."
        ),
    },
    {
        "id": "automation_modes",
        "title": "Automation modes",
        "keywords": ("full auto", "full-auto", "semi auto", "semi-auto", "approval", "human review", "自动", "审核"),
        "content": (
            "Full Auto lets the Agent complete preparation and execution when all policy safeguards pass, then report the result. "
            "Semi Auto freezes the exact recipient and content plan, waits for one confirmation before sending, and then completes the same execution path."
        ),
    },
    {
        "id": "safety_boundaries",
        "title": "Safety and commitments",
        "keywords": ("price", "pricing", "cost", "quote", "contract", "delivery", "promise", "价格", "报价", "合同", "交付"),
        "content": (
            "Pricing, delivery dates, contract terms, discounts, and special commitments are not fixed in the built-in knowledge base. "
            "The Agent should not invent or promise them; it should state that the scope needs to be confirmed and propose a next step."
        ),
    },
    {
        "id": "integration_scope",
        "title": "Current integration scope",
        "keywords": ("integration", "api", "gmail", "sync", "connect", "集成", "同步", "连接"),
        "content": (
            "This product is designed around Gmail synchronization and Gmail-thread replies. "
            "Specific external integrations or custom API work should be confirmed separately rather than assumed from the built-in scope."
        ),
    },
)


def _query_terms(query: str) -> set[str]:
    lowered = (query or "").lower()
    terms = set(re.findall(r"[a-z0-9][a-z0-9_-]{1,}", lowered))
    for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", lowered):
        # Chinese has no whitespace word boundaries. Bounded n-grams retain
        # phrases such as 销售邮件/自动跟进 without letting common single
        # characters dominate every chunk.
        for size in range(2, min(6, len(sequence)) + 1):
            terms.update(sequence[i:i + size] for i in range(len(sequence) - size + 1))
    return terms


def _score_text(query: str, text: str) -> int:
    lowered = (text or "").lower()
    terms = _query_terms(query)
    score = sum(min(6, len(term)) for term in terms if term in lowered)
    phrase = (query or "").strip().lower()
    if len(phrase) >= 4 and phrase in lowered:
        score += 20
    query_lowered = (query or "").lower()
    for aliases in _CONCEPT_ALIASES:
        query_hits = [alias for alias in aliases if alias in query_lowered]
        if not query_hits:
            continue
        text_hits = [alias for alias in aliases if alias in lowered]
        if text_hits:
            score += 12 + (4 * min(len(text_hits), 3))
    return score


def _chunks(content: str, max_chars: int = 1600) -> list[str]:
    """Split on semantic headings first, then keep each chunk bounded."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", content or "") if p.strip()]
    sections: list[str] = []
    current_section = ""
    heading_pattern = re.compile(r"^(?:【[^】]+】|#{1,6}\s+|案例\s*[A-ZＡ-Ｚ0-9]+[：:])")
    for paragraph in paragraphs:
        if heading_pattern.match(paragraph) and current_section:
            sections.append(current_section)
            current_section = paragraph
        else:
            current_section = f"{current_section}\n\n{paragraph}".strip()
    if current_section:
        sections.append(current_section)

    chunks: list[str] = []
    for section in sections:
        if len(section) <= max_chars:
            chunks.append(section)
            continue
        section_paragraphs = [p.strip() for p in re.split(r"\n\s*\n", section) if p.strip()]
        section_heading = section_paragraphs[0] if section_paragraphs else ""
        current = ""
        for paragraph in section_paragraphs:
            if current and len(current) + len(paragraph) + 2 > max_chars:
                chunks.append(current)
                current = f"{section_heading}\n\n{paragraph}" if paragraph != section_heading else paragraph
            else:
                current = f"{current}\n\n{paragraph}".strip()
        if current:
            chunks.append(current)
    return chunks


def retrieve_demo_knowledge(query: str, limit: int = 3) -> list[dict[str, Any]]:
    """Return relevant entries using transparent keyword scoring."""
    lowered = (query or "").lower()
    terms = set(re.findall(r"[a-z0-9][a-z0-9_-]{1,}|[\u4e00-\u9fff]", lowered))
    ranked: list[tuple[int, dict[str, Any]]] = []
    for entry in DEMO_KNOWLEDGE:
        score = 0
        for keyword in entry["keywords"]:
            key = keyword.lower()
            if key in lowered:
                score += 3 if " " in key or len(key) > 3 else 1
            if key in terms:
                score += 2
        if score:
            ranked.append((score, entry))
    ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [entry for _, entry in ranked[:limit]]


def retrieve_knowledge(query: str, db=None, owner_id: int | None = None, limit: int = 4) -> list[dict[str, Any]]:
    """Retrieve published user chunks, with built-in local knowledge as fallback."""
    ranked: list[tuple[int, dict[str, Any]]] = []
    if db is not None:
        from .. import models

        q = db.query(models.KnowledgeDocument).filter(
            models.KnowledgeDocument.status == "published",
            models.KnowledgeDocument.category != "reply_strategy",
        )
        if owner_id is not None:
            q = q.filter(models.KnowledgeDocument.owner_id == owner_id)
        for document in q.all():
            for index, chunk in enumerate(_chunks(document.content)):
                chunk_heading = chunk.splitlines()[0] if chunk else ""
                body_score = _score_text(query, chunk)
                heading_score = _score_text(query, chunk_heading)
                document_score = _score_text(query, f"{document.title} {document.category}")
                score = body_score + (heading_score * 2) + document_score
                if score:
                    ranked.append((score + 2, {
                        "id": f"doc:{document.id}:{index + 1}",
                        "document_id": document.id,
                        "title": document.title,
                        "category": document.category,
                        "content": chunk,
                        "source": document.source_name or document.source_type,
                        "version": document.version,
                        "score": score + 2,
                    }))

    for entry in retrieve_demo_knowledge(query, limit=limit):
        score = _score_text(query, f"{entry['title']}\n{entry['content']}")
        ranked.append((score, {**entry, "source": "builtin", "version": 1, "score": score}))

    ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [entry for _, entry in ranked[:limit]]


def get_reply_strategy(db=None, owner_id: int | None = None) -> dict[str, Any]:
    """Return the one published Workspace reply strategy or the safe default."""
    if db is not None:
        from .. import models

        query = db.query(models.KnowledgeDocument).filter_by(
            category="reply_strategy",
            status="published",
        )
        if owner_id is not None:
            query = query.filter(models.KnowledgeDocument.owner_id == owner_id)
        document = query.order_by(models.KnowledgeDocument.updated_at.desc()).first()
        if document is not None:
            return {
                "id": f"doc:{document.id}",
                "document_id": document.id,
                "title": document.title,
                "content": document.content,
                "source": document.source_name or document.source_type,
                "version": document.version,
                "is_default": False,
            }
    return {
        "id": "default_reply_strategy",
        "document_id": None,
        "title": "Built-in safe reply strategy",
        "content": DEFAULT_REPLY_STRATEGY,
        "source": "builtin",
        "version": 1,
        "is_default": True,
    }


def format_reply_strategy(db=None, owner_id: int | None = None) -> str:
    strategy = get_reply_strategy(db=db, owner_id=owner_id)
    return str(strategy["content"])


def format_knowledge_context(query: str, db=None, owner_id: int | None = None, limit: int = 4) -> str:
    entries = retrieve_knowledge(query, db=db, owner_id=owner_id, limit=limit)
    if not entries:
        return "No relevant approved knowledge-base entry was found."
    return "\n".join(
        f"[{entry['id']} v{entry.get('version', 1)}] {entry['title']}: {entry['content']}"
        for entry in entries
    )
