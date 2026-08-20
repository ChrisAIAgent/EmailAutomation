"""Gmail transport abstraction.

Two implementations:
  - RealGmailTransport   : wraps google-api-python-client (requires OAuth tokens)
  - InMemoryGmailTransport: deterministic in-memory double for tests / offline dev

All Gmail reads/writes go through the Unified Email Tool Layer -> transport.
"""
from __future__ import annotations

import abc
import base64
import email
import html
import json
import quopri
import re
from dataclasses import dataclass, field
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage as MimeMessage
from email.parser import BytesParser
from html.parser import HTMLParser
from typing import Any, Optional


class GmailTimeoutError(Exception):
    """Raised when a real Gmail network request exceeds GMAIL_HTTP_TIMEOUT_SECONDS.

    The message always begins with ``gmail_timeout:`` so the worker can mark the
    affected AutomationRun as ``partial``/``failed`` with a recognizable reason and
    the consumer can move on to the next task instead of wedging forever. This is a
    transport-layer signal distinct from the stale-run reclaim (which only covers
    consumer crashes, not network timeouts).
    """


@dataclass
class MessageDTO:
    gmail_message_id: str
    thread_id: str
    history_id: Optional[str]
    from_email: Optional[str]
    to_email: Optional[str]
    subject: Optional[str]
    snippet: Optional[str]
    body_text: Optional[str]
    body_html: Optional[str]
    is_incoming: bool
    received_at: Optional[str]
    message_id_header: Optional[str] = None
    in_reply_to_header: Optional[str] = None
    references_header: Optional[str] = None
    attachments_meta: list[dict] = field(default_factory=list)


@dataclass
class ThreadDTO:
    gmail_thread_id: str
    history_id: Optional[str]
    subject: Optional[str]
    snippet: Optional[str]
    messages: list[MessageDTO] = field(default_factory=list)


class _TextExtractor(HTMLParser):
    """Strip tags from HTML and keep readable text lines."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def get_text(self) -> str:
        return "\n".join(p.strip() for p in self.parts if p.strip())


def _html_to_text(html_text: str) -> str:
    """Convert HTML email body to readable plain text (UTF-8 safe)."""
    if not html_text:
        return ""
    try:
        parser = _TextExtractor()
        parser.feed(html_text)
        return html.unescape(parser.get_text())
    except Exception:
        return html.unescape(re.sub(r"<[^>]+>", " ", html_text))


def _text_corruption_score(value: str) -> int:
    """Score common damage caused by decoding UTF-8 as a single-byte charset."""
    replacements = value.count("\ufffd")
    c1_controls = sum(1 for char in value if "\x80" <= char <= "\x9f")
    mojibake_markers = sum(value.count(marker) for marker in ("Ã", "Â", "â€", "ðŸ"))
    return replacements * 10 + c1_controls * 4 + mojibake_markers * 2


def _repair_mojibake(value: str) -> str:
    """Reverse a safe Latin-1/UTF-8 mojibake round trip when it improves text."""
    if not value:
        return value
    try:
        repaired = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    return repaired if _text_corruption_score(repaired) < _text_corruption_score(value) else value


def _decode_header_value(value: str | None) -> str | None:
    if not value:
        return value
    try:
        return _repair_mojibake(str(make_header(decode_header(value))))
    except (LookupError, UnicodeDecodeError, ValueError):
        return _repair_mojibake(value)


def _looks_corrupt_text(value: str | None) -> bool:
    """Detect replacement characters or reversible UTF-8/Latin-1 mojibake."""
    if not value:
        return False
    replacements = value.count("\ufffd")
    replacement_damage = replacements >= 2 and replacements / max(1, len(value)) >= 0.08
    return replacement_damage or _repair_mojibake(value) != value


_repair_text_mojibake = _repair_mojibake


def _urlsafe_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _decode_body(part: dict) -> tuple[Optional[str], Optional[str]]:
    text, html = None, None

    def _charset(payload: dict) -> str | None:
        headers = {
            item.get("name", "").lower(): item.get("value", "")
            for item in payload.get("headers", [])
        }
        match = re.search(
            r"charset\s*=\s*[\"']?([^;\"'\s]+)",
            headers.get("content-type", ""),
            re.IGNORECASE,
        )
        return match.group(1).strip() if match else None

    def _repair_mojibake(value: str) -> str:
        if not re.search(r"[ÃÂæçåäèéð]", value):
            return value
        try:
            repaired = value.encode("latin-1").decode("utf-8")
            return repaired if repaired.count("�") <= value.count("�") else value
        except (UnicodeEncodeError, UnicodeDecodeError):
            return value

    def _get(b: Any, payload: dict) -> str:
        if isinstance(b, str):
            return _repair_text_mojibake(b)
        if isinstance(b, bytes):
            declared = (_charset(payload) or "").lower()
            candidates = [declared, "utf-8", "gb18030", "big5", "latin-1"]
            seen: set[str] = set()
            for charset in candidates:
                if not charset or charset in seen:
                    continue
                seen.add(charset)
                try:
                    decoded = _repair_text_mojibake(b.decode(charset))
                    if not _looks_corrupt_text(decoded):
                        return decoded
                except (LookupError, UnicodeDecodeError):
                    continue
            return b.decode("utf-8", "replace")
        return ""

    def walk(payload: dict):
        nonlocal text, html
        mime = payload.get("mimeType", "")
        body = payload.get("body", {})
        if mime == "text/plain" and body.get("data"):
            text = _get(_urlsafe_decode(body["data"]), payload)
        elif mime == "text/html" and body.get("data"):
            html = _get(_urlsafe_decode(body["data"]), payload)
        for sub in payload.get("parts", []):
            walk(sub)

    walk(part)
    return text, html


def parse_gmail_raw_message(msg: dict, owner_email: Optional[str] = None) -> MessageDTO:
    """Parse Gmail ``format=raw`` using the stdlib MIME implementation."""
    raw = _urlsafe_decode(msg.get("raw") or "")
    parsed = BytesParser(policy=policy.default).parsebytes(raw)

    text: str | None = None
    html_body: str | None = None
    attachments: list[dict] = []
    parts = parsed.walk() if parsed.is_multipart() else [parsed]
    for part in parts:
        filename = part.get_filename()
        if filename:
            payload = part.get_payload(decode=True) or b""
            attachments.append({
                "filename": filename,
                "mimeType": part.get_content_type(),
                "size": len(payload),
                "attachmentId": None,
            })
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            value = part.get_content()
        except (LookupError, UnicodeDecodeError):
            payload = part.get_payload(decode=True) or b""
            declared = part.get_content_charset()
            value = ""
            for charset in (declared, "utf-8", "gb18030", "big5", "latin-1"):
                if not charset:
                    continue
                try:
                    value = payload.decode(charset)
                    if not _looks_corrupt_text(value):
                        break
                except (LookupError, UnicodeDecodeError):
                    continue
        if content_type == "text/plain" and text is None:
            text = _repair_text_mojibake(str(value))
        elif content_type == "text/html" and html_body is None:
            html_body = _repair_text_mojibake(str(value))

    if not text and html_body:
        text = _html_to_text(html_body)

    from_header = str(parsed.get("From") or "")
    to_header = str(parsed.get("To") or "")
    from_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", from_header)
    to_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", to_header)
    from_email = from_match.group(0) if from_match else from_header
    to_email = to_match.group(0) if to_match else to_header
    is_incoming = (
        bool(from_email and from_email.lower() != owner_email.lower())
        if owner_email
        else bool(from_email and from_email.lower() != to_header.lower())
    )
    return MessageDTO(
        gmail_message_id=msg.get("id", ""),
        thread_id=msg.get("threadId", ""),
        history_id=msg.get("historyId"),
        from_email=from_email,
        to_email=to_email,
        subject=_decode_header_value(str(parsed.get("Subject") or "")),
        snippet=(text or "")[:200],
        body_text=text,
        body_html=html_body,
        is_incoming=is_incoming,
        received_at=str(parsed.get("Date") or "") or None,
        message_id_header=str(parsed.get("Message-ID") or "") or None,
        in_reply_to_header=str(parsed.get("In-Reply-To") or "") or None,
        references_header=str(parsed.get("References") or "") or None,
        attachments_meta=attachments,
    )


def parse_gmail_message(msg: dict, owner_email: Optional[str] = None) -> MessageDTO:
    """Convert a Gmail API message resource into a normalized MessageDTO.

    `owner_email` is the authenticated account's address. It is used to decide
    direction correctly: a message whose From equals the owner is an OUTGOING
    message, otherwise it is INCOMING. This avoids the fragile `from != to`
    heuristic that wrongly marks the owner's own sent mail as incoming.
    """
    h = {hh["name"].lower(): hh["value"] for hh in msg.get("payload", {}).get("headers", [])}
    text, html = _decode_body(msg.get("payload", {}))
    snippet = _repair_text_mojibake(msg.get("snippet") or "") or None
    # HTML-only emails: derive readable text so the UI never shows raw markup.
    if not text and html:
        text = _html_to_text(html)
    if not text and snippet:
        text = snippet

    atts = []
    for p in msg.get("payload", {}).get("parts", []):
        if p.get("filename"):
            atts.append(
                {
                    "filename": p.get("filename"),
                    "mimeType": p.get("mimeType"),
                    "size": int(p.get("body", {}).get("size", 0) or 0),
                    "attachmentId": p.get("body", {}).get("attachmentId"),
                }
            )

    me = h.get("to", "")
    frm = h.get("from", "")
    from_email = frm
    to_email = me
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", frm or "")
    if m:
        from_email = m.group(0)
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", me or "")
    if m:
        to_email = m.group(0)

    if owner_email:
        is_incoming = bool(from_email and from_email.lower() != owner_email.lower())
    else:
        # fallback when owner unknown (tests / inbox demo): compare to the To header
        is_incoming = bool(from_email and from_email.lower() != me.lower())
    return MessageDTO(
        gmail_message_id=msg["id"],
        thread_id=msg.get("threadId", ""),
        history_id=msg.get("historyId"),
        from_email=from_email,
        to_email=to_email,
        subject=_decode_header_value(h.get("subject")),
        snippet=snippet,
        body_text=text,
        body_html=html,
        is_incoming=is_incoming,
        received_at=h.get("date"),
        message_id_header=h.get("message-id") or None,
        in_reply_to_header=h.get("in-reply-to") or None,
        references_header=h.get("references") or None,
        attachments_meta=atts,
    )


class GmailTransport(abc.ABC):
    @abc.abstractmethod
    def get_profile(self) -> dict: ...

    @abc.abstractmethod
    def list_threads(self, query: str, max_results: int = 20, page_token: Optional[str] = None, include_spam_trash: bool = False) -> tuple[list[ThreadDTO], Optional[str]]: ...

    @abc.abstractmethod
    def get_thread(self, thread_id: str) -> ThreadDTO: ...

    @abc.abstractmethod
    def get_message(self, message_id: str) -> MessageDTO: ...

    @abc.abstractmethod
    def create_draft(self, to: str, subject: str, body_text: str, body_html: str, thread_id: Optional[str] = None, in_reply_to: Optional[str] = None, references: Optional[str] = None) -> dict: ...

    @abc.abstractmethod
    def update_draft(self, draft_id: str, to: str, subject: str, body_text: str, body_html: str,
                     thread_id: Optional[str] = None, in_reply_to: Optional[str] = None,
                     references: Optional[str] = None) -> dict: ...

    @abc.abstractmethod
    def send_draft(self, draft_id: str) -> dict: ...

    @abc.abstractmethod
    def add_label(self, thread_id: str, label: str) -> None: ...

    @abc.abstractmethod
    def remove_label(self, thread_id: str, label: str) -> None: ...

    @abc.abstractmethod
    def archive_thread(self, thread_id: str) -> None: ...

    @abc.abstractmethod
    def list_history(self, start_history_id: str, label_id: Optional[str] = None) -> tuple[list[dict], Optional[str]]: ...


class InMemoryGmailTransport(GmailTransport):
    """Deterministic in-memory Gmail double.

    NOT a source of fake business data — it is a transport test double used when
    no real Google credentials are configured or in tests. The UI clearly shows
    'not connected' / offline mode so users never mistake this for real mail.
    """

    def __init__(self, account_email: str = "demo@example.com"):
        self.account_email = account_email
        self._threads: dict[str, ThreadDTO] = {}
        self._drafts: dict[str, dict] = {}
        self._counter = 0
        self._history: list[dict] = []
        self._history_id = 1000
        self._labels: dict[str, set[str]] = {}

    def _nid(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter:06d}"

    def get_profile(self) -> dict:
        return {"email": self.account_email, "historyId": str(self._history_id)}

    def _inject_thread(self, subject: str, from_email: str, body: str, to_email: Optional[str] = None) -> ThreadDTO:
        tid = self._nid("t")
        mid = self._nid("m")
        self._history_id += 1
        msg = MessageDTO(
            gmail_message_id=mid,
            thread_id=tid,
            history_id=str(self._history_id),
            from_email=from_email,
            to_email=to_email or self.account_email,
            subject=subject,
            snippet=body[:120],
            body_text=body,
            body_html="",
            is_incoming=(from_email != self.account_email),
            received_at="2026-01-01T00:00:00Z",
            message_id_header=f"<{mid}@inmemory.local>",
        )
        t = ThreadDTO(gmail_thread_id=tid, history_id=str(self._history_id), subject=subject, snippet=body[:120], messages=[msg])
        self._threads[tid] = t
        return t

    def seed_incoming(self, subject: str, from_email: str, body: str) -> ThreadDTO:
        return self._inject_thread(subject, from_email, body)

    def list_threads(self, query: str, max_results: int = 20, page_token: Optional[str] = None, include_spam_trash: bool = False):
        items = list(self._threads.values())
        if query:
            q = query.lower()
            items = [t for t in items if q in (t.subject or "").lower() or q in (t.snippet or "").lower()]
        return items[:max_results], None

    def get_thread(self, thread_id: str) -> ThreadDTO:
        return self._threads[thread_id]

    def get_message(self, message_id: str) -> MessageDTO:
        for t in self._threads.values():
            for m in t.messages:
                if m.gmail_message_id == message_id:
                    return m
        raise KeyError(message_id)

    def create_draft(self, to, subject, body_text, body_html, thread_id=None, in_reply_to=None, references=None) -> dict:
        did = self._nid("d")
        self._drafts[did] = {
            "id": did,
            "to": to,
            "subject": subject,
            "body_text": body_text,
            "body_html": body_html,
            "threadId": thread_id,
            "in_reply_to": in_reply_to,
            "references": references,
        }
        return {"id": did, "message": {"id": self._nid("m"), "threadId": thread_id or self._nid("t")}}

    def update_draft(self, draft_id, to, subject, body_text, body_html, thread_id=None,
                     in_reply_to=None, references=None) -> dict:
        self._drafts[draft_id] = {
            "id": draft_id,
            "to": to,
            "subject": subject,
            "body_text": body_text,
            "body_html": body_html,
            "threadId": thread_id,
            "in_reply_to": in_reply_to,
            "references": references,
        }
        return {"id": draft_id}

    def send_draft(self, draft_id) -> dict:
        d = self._drafts.pop(draft_id)
        mid = self._nid("m")
        # Simulate an outgoing message appearing in a thread
        tid = d.get("threadId") or self._nid("t")
        self._history_id += 1
        msg = MessageDTO(
            gmail_message_id=mid,
            thread_id=tid,
            history_id=str(self._history_id),
            from_email=self.account_email,
            to_email=d["to"],
            subject=d["subject"],
            snippet=d["body_text"][:120],
            body_text=d["body_text"],
            body_html=d.get("body_html", ""),
            is_incoming=False,
            received_at="2026-01-01T00:00:00Z",
            message_id_header=f"<{mid}@inmemory.local>",
            in_reply_to_header=d.get("in_reply_to"),
            references_header=d.get("references"),
        )
        if tid in self._threads:
            self._threads[tid].messages.append(msg)
        else:
            self._threads[tid] = ThreadDTO(gmail_thread_id=tid, history_id=str(self._history_id), subject=d["subject"], snippet=d["body_text"][:120], messages=[msg])
        self._history.append({"type": "send", "messageId": mid, "threadId": tid})
        return {"id": mid, "threadId": tid}

    def add_label(self, thread_id, label):
        self._labels.setdefault(thread_id, set()).add(label)

    def remove_label(self, thread_id, label):
        self._labels.get(thread_id, set()).discard(label)

    def archive_thread(self, thread_id):
        self._labels.get(thread_id, set()).discard("INBOX")

    def list_history(self, start_history_id, label_id=None):
        start = int(start_history_id or 0)
        events = [h for h in self._history if int(h.get("historyId", start)) > start] if False else []
        return self._history, str(self._history_id)


def build_mime(to: str, subject: str, body_text: str, body_html: str, thread_id: Optional[str], in_reply_to: Optional[str], references: Optional[str]) -> str:
    """Build RFC822 message; returns base64url raw string."""
    msg = MimeMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    if body_html:
        msg.set_content(body_text or "")
        msg.add_alternative(body_html, subtype="html")
    else:
        msg.set_content(body_text or "")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return raw
