"""Acceptance-readiness checks for the QA plan: Gmail parse correctness,
intent heuristic for the 7 test emails, and the OpenClaw-only guard."""
from __future__ import annotations

import base64
import os

from app.config import get_settings, is_openclaw_configured
from app.gmail.transport import parse_gmail_message, parse_gmail_raw_message
from app.agents.langgraph_agent import _heuristic_intent


def _setenv(**kw):
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()


def _gmail_msg(frm: str, to: str, subject: str, body: str, html: bool = False) -> dict:
    part = (
        {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(body.encode("utf-8")).decode()}}
        if html else
        {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(body.encode("utf-8")).decode()}}
    )
    return {
        "id": "m1", "threadId": "t1", "historyId": "1",
        "payload": {
            "headers": [
                {"name": "From", "value": frm},
                {"name": "To", "value": to},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Mon, 01 Jan 2026 00:00:00 +0000"},
            ],
            "parts": [part],
        },
        "snippet": body[:20],
    }


def test_incoming_vs_outgoing_direction():
    owner = "a@test.com"
    incoming = parse_gmail_message(_gmail_msg("B <b@test.com>", "A <a@test.com>", "hi", "hello"), owner_email=owner)
    assert incoming.is_incoming is True
    assert incoming.from_email == "b@test.com"
    assert incoming.to_email == "a@test.com"

    outgoing = parse_gmail_message(_gmail_msg("A <a@test.com>", "B <b@test.com>", "hi", "hello"), owner_email=owner)
    # The owner's OWN sent mail must NOT be marked incoming (was the old bug).
    assert outgoing.is_incoming is False


def test_chinese_body_no_garble():
    msg = _gmail_msg("B <b@test.com>", "A <a@test.com>", "报价", "中文正文测试，没有乱码。")
    dto = parse_gmail_message(msg, owner_email="a@test.com")
    assert "中文正文测试" in (dto.body_text or "")


def test_rfc2047_chinese_subject_is_decoded():
    encoded_subject = "=?UTF-8?B?5Lit5paH6YKu5Lu25rWL6K+V?="
    msg = _gmail_msg(
        "B <b@test.com>", "A <a@test.com>", encoded_subject, "正文正常。"
    )
    dto = parse_gmail_message(msg, owner_email="a@test.com")
    assert dto.subject == "中文邮件测试"


def test_raw_mime_gb18030_body_and_subject_are_decoded():
    subject = "企业AI邮件咨询"
    body = "我们希望使用 AI 自动处理销售邮件，并连接现有 CRM。"
    encoded_subject = base64.b64encode(subject.encode("utf-8")).decode("ascii")
    encoded_body = base64.b64encode(body.encode("gb18030")).decode("ascii")
    mime = (
        "From: Person <person@example.com>\r\n"
        "To: Owner <owner@example.com>\r\n"
        f"Subject: =?UTF-8?B?{encoded_subject}?=\r\n"
        "Date: Mon, 28 Jul 2026 14:00:00 +0800\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=gb18030\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
        f"{encoded_body}\r\n"
    ).encode("ascii")
    gmail_raw = base64.urlsafe_b64encode(mime).decode("ascii").rstrip("=")
    dto = parse_gmail_raw_message({
        "id": "raw-cn",
        "threadId": "thread-cn",
        "historyId": "8",
        "raw": gmail_raw,
    }, owner_email="owner@example.com")
    assert dto.subject == subject
    assert dto.body_text.strip() == body
    assert dto.is_incoming is True


def test_raw_mime_repairs_utf8_body_mislabeled_as_latin1():
    subject = "\u4e2d\u6587\u90ae\u4ef6\u6d4b\u8bd5"
    body = "\u6211\u4eec\u5e0c\u671b\u4f7f\u7528 AI \u81ea\u52a8\u5904\u7406\u9500\u552e\u90ae\u4ef6\uff0c\u5e76\u8fde\u63a5\u73b0\u6709 CRM\u3002"
    encoded_subject = base64.b64encode(subject.encode("utf-8")).decode("ascii")
    encoded_body = base64.b64encode(body.encode("utf-8")).decode("ascii")
    mime = (
        "From: Person <person@example.com>\r\n"
        "To: Owner <owner@example.com>\r\n"
        f"Subject: =?UTF-8?B?{encoded_subject}?=\r\n"
        "Date: Mon, 28 Jul 2026 14:00:00 +0800\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=iso-8859-1\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
        f"{encoded_body}\r\n"
    ).encode("ascii")
    dto = parse_gmail_raw_message({
        "id": "raw-mislabeled-cn",
        "threadId": "thread-mislabeled-cn",
        "historyId": "9",
        "raw": base64.urlsafe_b64encode(mime).decode("ascii").rstrip("="),
    }, owner_email="owner@example.com")

    assert dto.subject == subject
    assert dto.body_text.strip() == body
    assert dto.snippet == body


def test_html_body_extracted_as_readable_text():
    msg = _gmail_msg("B <b@test.com>", "A <a@test.com>", "html", "<p>Hello <b>World</b></p><div>Line2</div>", html=True)
    dto = parse_gmail_message(msg, owner_email="a@test.com")
    assert dto.body_text and "<p>" not in dto.body_text and "Hello" in dto.body_text and "World" in dto.body_text
    # raw html still retained for reference
    assert dto.body_html and "<p>" in dto.body_html


def test_html_body_extraction_omits_style_script_and_jsonld():
    html_body = """
    <html><head><style>@import url('https://fonts.example'); body { color: red; }</style>
    <script type='application/ld+json'>{\"@type\":\"EmailMessage\"}</script></head>
    <body><p>Hello <b>Customer</b></p><p>Useful update</p></body></html>
    """
    dto = parse_gmail_message(
        _gmail_msg("B <b@test.com>", "A <a@test.com>", "html", html_body, html=True),
        owner_email="a@test.com",
    )
    assert "Hello Customer" in dto.body_text
    assert "Useful update" in dto.body_text
    assert "@import" not in dto.body_text
    assert "color: red" not in dto.body_text
    assert "EmailMessage" not in dto.body_text


def test_intent_heuristic_matches_qa_emails():
    cases = {
        "有兴趣，请发一下报价。": "interested",
        "你们支持哪些 CRM？": "asking_question",
        "价格可能太高了。": "objection",
        "暂时不需要。": "not_interested",
        "请不要再联系我，取消订阅。": "unsubscribe",
        "您好，我正在休假，这是自动回复。": "out_of_office",
        "您的月度账单已生成，请查收。": "unknown",
    }
    for text, expected in cases.items():
        intent, conf = _heuristic_intent(text)
        assert intent == expected, f"text={text!r} -> {intent}, expected {expected}"
        assert 0.0 <= conf <= 1.0


def test_openclaw_only_guard_blocks_when_unconfigured(client):
    _setenv(OPENCLAW_ENDPOINT=None, OPENCLAW_COMMAND=None)
    assert is_openclaw_configured() is False
    r = client.post("/api/campaigns", json={"name": "X", "agent_mode": "openclaw_only", "primary_agent": "openclaw"})
    assert r.status_code == 400, r.text
    assert "OpenClaw" in r.json()["detail"]


def test_openclaw_only_allowed_when_configured(client):
    _setenv(OPENCLAW_ENDPOINT="http://localhost:9999/openclaw")
    assert is_openclaw_configured() is True
    r = client.post("/api/campaigns", json={"name": "X", "agent_mode": "openclaw_only", "primary_agent": "openclaw"})
    assert r.status_code == 200, r.text
    _setenv(OPENCLAW_ENDPOINT=None)
