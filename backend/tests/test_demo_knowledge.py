from app.knowledge import (
    format_knowledge_context,
    get_reply_strategy,
    retrieve_demo_knowledge,
    retrieve_knowledge,
)
from app import models
from types import SimpleNamespace


def test_demo_knowledge_retrieves_product_and_reply_context():
    rows = retrieve_demo_knowledge("How does Gmail inbox reply automation work?")
    ids = {row["id"] for row in rows}
    assert "product_overview" in ids
    assert "inbox_reply" in ids


def test_demo_knowledge_does_not_fabricate_unknown_topics():
    assert retrieve_demo_knowledge("quantum accounting for Mars") == []
    assert "No relevant" in format_knowledge_context("quantum accounting for Mars")


def test_demo_knowledge_surfaces_commitment_boundary():
    rows = retrieve_demo_knowledge("Can you guarantee pricing and delivery date?")
    assert rows[0]["id"] == "safety_boundaries"
    assert "should not invent" in rows[0]["content"]


def test_user_knowledge_draft_is_not_retrieved_until_published(client, db):
    created = client.post("/api/knowledge", json={
        "title": "Orchid Support Policy",
        "category": "support",
        "content": "Orchid incidents receive a response within four business hours.",
        "publish": False,
    })
    assert created.status_code == 200
    document_id = created.json()["id"]

    before = retrieve_knowledge("Orchid incident response", db=db, owner_id=1)
    assert all(row.get("document_id") != document_id for row in before)

    published = client.post(f"/api/knowledge/{document_id}/publish")
    assert published.status_code == 200
    after = retrieve_knowledge("Orchid incident response", db=db, owner_id=1)
    assert after[0]["document_id"] == document_id
    assert after[0]["source"] == "pasted"


def test_knowledge_categories_remain_customer_defined(client):
    created = client.post("/api/knowledge", json={
        "title": "Regional fulfillment policy",
        "category": "logistics_region_rules",
        "content": "Use this only for confirmed regions.",
        "publish": False,
    })
    assert created.status_code == 200, created.text
    document_id = created.json()["id"]
    assert created.json()["category"] == "logistics_region_rules"

    updated = client.put(f"/api/knowledge/{document_id}", json={
        "category": "regulated_market_requirements",
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()["category"] == "regulated_market_requirements"


def test_markdown_upload_and_search_api(client):
    uploaded = client.post(
        "/api/knowledge/upload",
        data={"category": "product", "publish": "true"},
        files={"file": ("atlas.md", b"# Atlas\n\nAtlas supports verified mailbox routing.", "text/markdown")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["source_type"] == "markdown"
    assert uploaded.json()["status"] == "published"

    searched = client.post("/api/knowledge/search", json={"query": "Atlas mailbox routing"})
    assert searched.status_code == 200
    assert searched.json()["results"][0]["title"] == "atlas"


def test_duplicate_knowledge_content_is_rejected(client):
    payload = {"title": "One", "content": "A unique reusable knowledge statement.", "publish": True}
    assert client.post("/api/knowledge", json=payload).status_code == 200
    duplicate = client.post("/api/knowledge", json={**payload, "title": "Two"})
    assert duplicate.status_code == 409


def test_sales_email_query_prioritizes_matching_case_chunk(client, db):
    content = """# TAC AISolution 企业 AI 应用落地知识库

【九、典型客户问题与建议回答】

问：为什么不直接买一个大模型？
答：企业还需要业务流程、知识库和权限审批。

【十一、TAC AISolution Demo 案例】

案例 A：销售邮件自动化、客户自动跟进与企业邮箱 Agent
- 检索关键词：销售邮件、邮件自动化、邮箱自动化、客户跟进、自动跟进、邮件回复
- TAC 方案：接入企业邮箱，进行邮件分拣、意图识别、个性化回复和跟进任务管理。

案例 C：企业流程 Agent 自动化
- TAC 方案：连接 ERP 和审批系统，设计跨系统工作流。
"""
    created = client.post("/api/knowledge", json={
        "title": "TAC Sales Automation",
        "category": "sales",
        "content": content,
        "publish": True,
    })
    assert created.status_code == 200

    rows = retrieve_knowledge(
        "TAC AISolution 如何帮助企业实现销售邮件自动化和客户自动跟进？",
        db=db,
        owner_id=1,
    )
    assert "案例 A：销售邮件自动化、客户自动跟进" in rows[0]["content"]
    assert rows[0]["score"] > rows[1]["score"]


def test_get_single_document(client):
    created = client.post("/api/knowledge", json={
        "title": "Single Get Policy", "category": "support",
        "content": "A document that should be retrievable by id.", "publish": True})
    assert created.status_code == 200
    document_id = created.json()["id"]

    got = client.get(f"/api/knowledge/{document_id}")
    assert got.status_code == 200
    assert got.json()["id"] == document_id
    assert got.json()["content"] == "A document that should be retrievable by id."

    missing = client.get("/api/knowledge/999999")
    assert missing.status_code == 404


def test_delete_permanently_removes_document_and_keeps_audit(client, db):
    created = client.post("/api/knowledge", json={
        "title": "Delete Me", "category": "support",
        "content": "This document will be permanently deleted.", "publish": True})
    assert created.status_code == 200
    document_id = created.json()["id"]

    deleted = client.delete(f"/api/knowledge/{document_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"id": document_id, "deleted": True}

    default_list = client.get("/api/knowledge")
    assert all(d["id"] != document_id for d in default_list.json())
    assert client.get(f"/api/knowledge/{document_id}").status_code == 404
    audit = db.query(models.AuditLog).filter_by(
        action="knowledge_deleted", entity_id=str(document_id)
    ).one()
    assert "Delete Me" in audit.detail
    assert "permanently deleted" not in audit.detail


def test_one_published_reply_strategy_and_fact_retrieval_are_separate(client, db):
    draft = client.post("/api/knowledge", json={
        "title": "Reply strategy draft",
        "category": "reply_strategy",
        "content": "Acknowledge the request, answer known facts, then offer one next step.",
        "publish": False,
    })
    assert draft.status_code == 200
    assert get_reply_strategy(db=db, owner_id=1)["is_default"] is True

    published = client.post(f"/api/knowledge/{draft.json()['id']}/publish")
    assert published.status_code == 200
    strategy = get_reply_strategy(db=db, owner_id=1)
    assert strategy["document_id"] == draft.json()["id"]
    assert strategy["is_default"] is False
    assert all(
        row.get("document_id") != draft.json()["id"]
        for row in retrieve_knowledge("answer known facts", db=db, owner_id=1)
    )

    conflict = client.post("/api/knowledge", json={
        "title": "Conflicting strategy",
        "category": "reply_strategy",
        "content": "A different global strategy that must not be published concurrently.",
        "publish": True,
    })
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "published_reply_strategy_already_exists"


def test_published_reply_strategy_is_injected_before_fact_knowledge(client, db, monkeypatch):
    from app.agents import langgraph_agent

    created = client.post("/api/knowledge", json={
        "title": "Global strategy",
        "category": "reply_strategy",
        "content": "STRATEGY_MARKER: acknowledge the request and give one next step.",
        "publish": True,
    })
    assert created.status_code == 200
    fact = client.post("/api/knowledge", json={
        "title": "CRM facts",
        "category": "sales_knowledge",
        "content": "FACT_MARKER: CRM compatibility requires API and permission review.",
        "publish": True,
    })
    assert fact.status_code == 200

    captured = {}
    class FakeLLM:
        def with_structured_output(self, _):
            return self
        def invoke(self, prompt):
            captured["prompt"] = prompt
            return SimpleNamespace(body_text="We can review the CRM API and permissions, then confirm one next step.")

    monkeypatch.setattr(langgraph_agent, "_make_llm", lambda **_: FakeLLM())
    monkeypatch.setattr(langgraph_agent._DB_HOLDER, "db", db, raising=False)

    result = langgraph_agent._llm_reply(
        {"owner_id": 1, "first_name": "Lee"},
        {},
        "Can you connect to our CRM?",
        "Customer: We use a CRM with an API.",
    )

    assert result
    assert "STRATEGY_MARKER" in captured["prompt"]
    assert "FACT_MARKER" in captured["prompt"]
    assert captured["prompt"].index("STRATEGY_MARKER") < captured["prompt"].index("FACT_MARKER")
