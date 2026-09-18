import json
import zipfile
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from app import models
from app.api.inbox import _auto_contact_from_inbox
from app.services.xlsx import read_xlsx_rows


def _campaign(client):
    response = client.post("/api/campaigns", json={"name": "CRM Campaign"})
    assert response.status_code == 200
    return response.json()["id"]


def test_contact_crud_requires_email_and_name(client):
    assert client.post("/api/contacts", json={"email": "lead@example.com"}).status_code == 422
    company_only = client.post("/api/contacts", json={"email": "company@example.com", "company": "Acme"})
    assert company_only.status_code == 422
    assert company_only.json()["detail"] == "name_required"
    created = client.post("/api/contacts", json={
        "email": "LEAD@example.com", "first_name": "Lead", "company": "Acme", "category": "qualified",
        "intent_level": "high", "tags": ["SaaS", "priority"], "segments": ["Finance", "IT"],
    })
    assert created.status_code == 200, created.text
    row = created.json()
    assert row["email"] == "lead@example.com"
    assert row["tags"] == ["SaaS", "priority"]
    assert row["segments"] == ["Finance", "IT"]
    assert client.post("/api/contacts", json={"email": "lead@example.com", "first_name": "Lead"}).status_code == 409
    updated = client.put(f"/api/contacts/{row['id']}", json={
        "email": "lead@example.com", "first_name": "Lin", "company": "Acme",
        "category": "customer", "intent_level": "medium", "tags": ["customer"],
    })
    assert updated.status_code == 200
    assert updated.json()["category"] == "customer"


def test_contact_file_import_preview_then_confirm_persists_template_fields(client):
    csv_data = (
        b'email,first_name,last_name,company,title,phone,website,segments,tags,timezone,notes,custom_fields,source\n'
        b'new@example.com,New,Person,Acme,Founder,+1 555 0100,https://acme.example,"Finance; IT; finance","saas; priority",Asia/Shanghai,Met at event,"{""account_tier"":""enterprise""}",event_import\n'
        b'company-only@example.com,,,Company Only,,,,,,,,,\n'
        b'broken-fields@example.com,Broken,Fields,,,,,,,,,"not-json",\n'
    )
    preview = client.post("/api/contacts/import?confirm=false", files={"file": ("contacts.csv", BytesIO(csv_data), "text/csv")})
    assert preview.status_code == 200
    assert preview.json()["preview"]["valid"] == 1
    assert preview.json()["preview"]["invalid"] == 2
    assert {tuple(row["errors"]) for row in preview.json()["preview"]["errors"]} == {
        ("name_required",), ("invalid_custom_fields",),
    }
    confirmed = client.post("/api/contacts/import?confirm=true", files={"file": ("contacts.csv", BytesIO(csv_data), "text/csv")})
    assert confirmed.status_code == 200
    assert confirmed.json()["imported"] == 1
    contact = client.get("/api/contacts").json()[0]
    assert contact["first_name"] == "New"
    assert contact["last_name"] == "Person"
    assert contact["phone"] == "+1 555 0100"
    assert contact["website"] == "https://acme.example"
    assert contact["segments"] == ["Finance", "IT"]
    assert contact["tags"] == ["saas", "priority"]
    assert contact["timezone"] == "Asia/Shanghai"
    assert contact["notes"] == "Met at event"
    assert contact["custom_fields"] == {"account_tier": "enterprise"}
    assert contact["source"] == "event_import"


def test_contact_import_accepts_chinese_aliases(client):
    custom_json = json.dumps({"region": "CN"}).replace('"', '""')
    csv_data = (
        "邮箱,名,姓,客户分类,标签,自定义字段,网站,时区,来源\n"
        f'li@example.com,Li,Ming,金融；IT,重点客户，会议,"{custom_json}",https://example.cn,Asia/Shanghai,excel_import\n'
    ).encode("utf-8")
    response = client.post("/api/contacts/import?confirm=true", files={
        "file": ("contacts.csv", BytesIO(csv_data), "text/csv")
    })
    assert response.status_code == 200, response.text
    contact = client.get("/api/contacts").json()[0]
    assert contact["segments"] == ["金融", "IT"]
    assert contact["tags"] == ["重点客户", "会议"]
    assert contact["custom_fields"] == {"region": "CN"}
    assert contact["website"] == "https://example.cn"
    assert contact["timezone"] == "Asia/Shanghai"
    assert contact["source"] == "excel_import"


def test_contact_excel_template_matches_import_contract(client):
    response = client.get("/api/contacts/template.xlsx")
    assert response.status_code == 200
    assert "contacts-import-template.xlsx" in response.headers["content-disposition"]
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
        assert "Contacts" in workbook_xml and "Field Guide" in workbook_xml
    rows = read_xlsx_rows(response.content)
    headers = rows[0]
    assert headers == [
        "email", "first_name", "last_name", "system_category", "segments", "tags",
        "company", "title", "phone", "website", "timezone", "notes", "custom_fields", "source",
    ]
    assert rows[1][3] == "prospect"
    assert rows[1][4] == "Finance; IT"
    imported = client.post("/api/contacts/import?confirm=true", files={
        "file": ("contacts-import-template.xlsx", BytesIO(response.content), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    })
    assert imported.status_code == 200, imported.text
    contact = client.get("/api/contacts").json()[0]
    assert contact["segments"] == ["Finance", "IT"]


def test_campaign_selects_existing_contacts_and_skips_suppressed(client, db):
    campaign_id = _campaign(client)
    first = client.post("/api/contacts", json={"email": "ok@example.com", "first_name": "OK"}).json()
    blocked = client.post("/api/contacts", json={"email": "stop@example.com", "first_name": "Stop", "company": "Stop Ltd"}).json()
    db.add(models.Suppression(owner_id=1, email="stop@example.com", reason="unsubscribe"))
    db.commit()
    response = client.post(f"/api/campaigns/{campaign_id}/contacts", json={"contact_ids": [first["id"], blocked["id"]]})
    assert response.status_code == 200
    assert response.json()["added"] == 1
    assert response.json()["skipped"][0]["reason"] == "suppressed"
    assert client.post(f"/api/campaigns/{campaign_id}/contacts", json={"contact_ids": [first["id"]]}).json()["added"] == 0


def test_campaign_file_upload_accepts_contact_template_fields(client):
    campaign_id = _campaign(client)
    csv_data = (
        b'email,first_name,last_name,company,title,phone,website,segments,tags,timezone,notes,custom_fields,source\n'
        b'campaign@example.com,Campaign,Lead,Acme,VP,+1 555 0110,https://acme.example,"Finance, IT",priority; webinar,Asia/Shanghai,Campaign upload,"{""account_tier"":""enterprise""}",event_import\n'
    )
    preview = client.post(
        f"/api/campaigns/{campaign_id}/upload-contacts",
        files={"file": ("contacts.csv", BytesIO(csv_data), "text/csv")},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["preview"]["valid"] == 1
    confirmed = client.post(
        f"/api/campaigns/{campaign_id}/upload-contacts?confirm=true",
        files={"file": ("contacts.csv", BytesIO(csv_data), "text/csv")},
    )
    assert confirmed.status_code == 200, confirmed.text
    contact = client.get("/api/contacts").json()[0]
    assert contact["segments"] == ["Finance", "IT"]
    assert contact["tags"] == ["priority", "webinar"]
    assert contact["phone"] == "+1 555 0110"
    assert contact["custom_fields"] == {"account_tier": "enterprise"}
    assert contact["source"] == "event_import"


def test_inbox_sender_can_be_promoted_to_contact(client, db):
    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=True)
    db.add(account); db.flush()
    thread = models.EmailThread(gmail_account_id=account.id, gmail_thread_id="crm-thread", contact_email="sender@example.com")
    db.add(thread); db.commit()
    response = client.post(f"/api/inbox/threads/{thread.id}/contact", json={
        "first_name": "Sender", "category": "qualified", "tags": ["inbox"]
    })
    assert response.status_code == 200
    assert response.json()["created"] is True
    assert client.get("/api/inbox/threads").json()[0]["is_contact"] is True


def _inbox_message(db, suffix, sender):
    account = db.query(models.GmailAccount).first()
    if not account:
        account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=True)
        db.add(account)
        db.flush()
    thread = models.EmailThread(
        gmail_account_id=account.id,
        gmail_thread_id=f"auto-{suffix}",
        contact_email=sender,
    )
    db.add(thread)
    db.flush()
    message = models.EmailMessage(
        thread_id=thread.id,
        gmail_message_id=f"message-{suffix}",
        from_email=sender,
        to_email=account.email,
        is_incoming=True,
        body_text="test",
    )
    db.add(message)
    db.flush()
    return thread, message


def test_sort_auto_creates_eligible_contacts_without_duplicates(db):
    thread, message = _inbox_message(db, "valid", "buyer.person@example.com")
    first = _auto_contact_from_inbox(db, thread, "interested", message)
    second = _auto_contact_from_inbox(db, thread, "interested", message)
    assert first.id == second.id
    assert first.category == "qualified"
    assert first.source == "inbox_auto"
    assert db.query(models.Contact).filter_by(email="buyer.person@example.com").count() == 1


def test_sort_does_not_create_unknown_rejected_or_irrelevant_contacts(db):
    rejected_thread, rejected_message = _inbox_message(db, "reject", "reject@example.com")
    rejected = _auto_contact_from_inbox(db, rejected_thread, "not_interested", rejected_message)
    assert rejected is None
    assert db.query(models.Contact).filter_by(email="reject@example.com").first() is None

    spam_thread, spam_message = _inbox_message(db, "spam", "marketing@example.com")
    assert _auto_contact_from_inbox(db, spam_thread, "unknown", spam_message) is None
    assert db.query(models.Contact).filter_by(email="marketing@example.com").first() is None


def test_inbox_groups_multiple_threads_under_one_contact(client, db):
    contact = models.Contact(
        owner_id=1, email="group@example.com", first_name="Grace",
        company="Grouped Inc", category="qualified", tags='["priority"]',
        lifecycle_stage="needs_reply", next_action="reply",
    )
    db.add(contact)
    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=True)
    db.add(account); db.flush()
    for index in (1, 2):
        thread = models.EmailThread(
            gmail_account_id=account.id, gmail_thread_id=f"group-{index}",
            contact_email=contact.email, subject=f"Conversation {index}",
            intent="asking_question",
        )
        db.add(thread); db.flush()
        db.add(models.EmailMessage(
            thread_id=thread.id, gmail_message_id=f"group-message-{index}",
            from_email=contact.email, to_email=account.email,
            is_incoming=True, body_text=f"Question {index}",
        ))
    db.commit()
    response = client.get("/api/inbox/customers")
    assert response.status_code == 200
    row = response.json()[0]
    assert row["email"] == contact.email
    assert row["first_name"] == "Grace"
    assert row["company"] == "Grouped Inc"
    assert row["thread_count"] == 2
    assert row["message_count"] == 2
    assert row["next_action"] == "reply"


def test_agent_does_not_overwrite_manually_locked_contact(client, db):
    contact = models.Contact(
        owner_id=1, email="locked@example.com", company="Locked Inc",
        category="customer", intent_level="low", lifecycle_stage="contacted",
        next_action="human_review", manual_lock=True,
    )
    db.add(contact)
    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=True)
    db.add(account); db.flush()
    thread = models.EmailThread(
        gmail_account_id=account.id, gmail_thread_id="locked-thread",
        contact_email=contact.email, subject="Interested",
    )
    db.add(thread); db.flush()
    db.add(models.EmailMessage(
        thread_id=thread.id, gmail_message_id="locked-message",
        from_email=contact.email, to_email=account.email,
        is_incoming=True, body_text="I am interested",
    ))
    db.commit()
    decision = SimpleNamespace(
        intent="interested", summary="Interested", recommended_action="reply",
        reasoning_summary="Explicit interest", model="test",
    )
    with patch("app.api.inbox.Orchestrator.analyze", return_value=decision):
        response = client.post(f"/api/inbox/threads/{thread.id}/analyze")
    assert response.status_code == 200
    db.refresh(contact)
    assert contact.category == "customer"
    assert contact.intent_level == "low"
    assert contact.lifecycle_stage == "contacted"
    assert contact.next_action == "human_review"
