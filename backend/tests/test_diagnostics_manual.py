"""Regression tests for the manual, on-demand diagnostics chain.

Scope (per the stabilization round):
- every collector stays read-only: no directory creation, no write probes;
- database misreporting is fixed (quick_check ok => healthy; errors/unknown
  never reported as healthy);
- evidence never leaks tokens, emails, paths, OAuth URL params;
- Gmail checks only configuration/state (no refresh, no Gmail calls);
- an empty RESTRICTED_RECIPIENT_ALLOWLIST is a supported configuration;
- collector isolation, trace-id scoping, and the "no polling" guarantee.
"""
from __future__ import annotations

import json
import os
import types
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import diagnostics, redact
from app.api import system as system_api
from app.config import _DATA, get_settings
from app.models import Automation, GmailAccount, OAuthCredential
import app.diagnostics_investigate as di


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_connected_account(
    db,
    email: str = "pyx1171898390@gmail.com",
    history_id: str = "12345",
    expiry: datetime | None = None,
    refresh: bool = True,
    access: bool = True,
):
    account = GmailAccount(user_id=1, email=email, is_connected=True, history_id=history_id)
    db.add(account)
    db.flush()
    cred = OAuthCredential(
        gmail_account_id=account.id,
        access_token_enc="enc-access" if access else None,
        refresh_token_enc="enc-refresh" if refresh else None,
        token_expiry=expiry,
    )
    db.add(cred)
    db.commit()
    return account


class _FakeURL:
    def __init__(self, database: str):
        self.database = database

    def get_backend_name(self) -> str:
        return "sqlite"


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeDB:
    """Minimal session stand-in for the database collector."""

    def __init__(self, database: str, quick_check_row=("ok",), quick_check_exc: Exception | None = None):
        self.database = database
        self.quick_check_row = quick_check_row
        self.quick_check_exc = quick_check_exc

    def get_bind(self):
        return types.SimpleNamespace(url=_FakeURL(self.database))

    def execute(self, query, *args, **kwargs):
        sql = str(query)
        if "quick_check" in sql:
            if self.quick_check_exc is not None:
                raise self.quick_check_exc
            return _FakeResult(self.quick_check_row)
        return _FakeResult(("ok",))


class _FakeInspector:
    def __init__(self, bind):
        pass

    def get_table_names(self):
        return ["gmail_accounts", "contacts", "campaigns", "approvals"]


@pytest.fixture
def fake_inspector(monkeypatch):
    monkeypatch.setattr(di, "inspect", lambda bind: _FakeInspector(bind))


@pytest.fixture
def no_network(monkeypatch):
    def _forbidden(*args, **kwargs):
        raise AssertionError("diagnostics must never perform network calls")

    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)


# ---------------------------------------------------------------------------
# 1. All targets + unknown target
# ---------------------------------------------------------------------------
def test_every_target_and_unknown_target(db, no_network):
    for name in di._COLLECTORS:
        report = di.investigate(db, target=name)["reports"][0]
        assert report["target"] == name
        assert report["status"] in {"healthy", "confirmed", "suspected", "unknown"}

    report = di.investigate(db, target="no.such_target")["reports"][0]
    assert report["status"] == "unknown"
    assert "no.such_target" in report["summary"]


def test_unknown_target_lists_valid_targets(db, no_network):
    report = di.investigate(db, target="zzz")["reports"][0]
    assert set(di._COLLECTORS).issubset(set(report["evidence"][0]["finding"].replace(" ", "").split(",")))


# ---------------------------------------------------------------------------
# 2. Database: quick_check semantics
# ---------------------------------------------------------------------------
def test_db_quick_check_ok_is_healthy(tmp_path, fake_inspector):
    db_file = tmp_path / "app.db"
    db_file.write_bytes(b"")
    report = di.investigate(FakeDB(str(db_file)), target="database.reachable")["reports"][0]
    assert report["status"] == "healthy"
    assert any(e["finding"] == "ok" and e["source"] == "pragma_quick_check" for e in report["evidence"])


def test_db_corrupted_is_confirmed_not_healthy(tmp_path, fake_inspector):
    db_file = tmp_path / "app.db"
    db_file.write_bytes(b"")
    report = di.investigate(
        FakeDB(str(db_file), quick_check_row=("*** in page 3",)), target="database.reachable"
    )["reports"][0]
    assert report["status"] == "confirmed"
    assert report["root_cause"] == "db_integrity_error"


def test_db_check_exception_is_unknown_never_healthy(tmp_path, fake_inspector):
    db_file = tmp_path / "app.db"
    db_file.write_bytes(b"")
    report = di.investigate(
        FakeDB(str(db_file), quick_check_exc=RuntimeError("database is locked")),
        target="database.reachable",
    )["reports"][0]
    assert report["status"] == "unknown"
    assert report["root_cause"] == "db_check_indeterminate"
    assert "无法确认根因" in report["summary"]


def test_db_file_missing_is_confirmed(tmp_path, fake_inspector):
    report = di.investigate(FakeDB(str(tmp_path / "nope.db")), target="database.reachable")["reports"][0]
    assert report["status"] == "confirmed"
    assert report["root_cause"] == "db_file_missing"


# ---------------------------------------------------------------------------
# 3. Overview failures must never read as healthy
# ---------------------------------------------------------------------------
def test_system_target_unknown_when_overview_raises(db, monkeypatch, no_network):
    def boom(db_):
        raise RuntimeError("overview exploded")

    monkeypatch.setattr(di, "run_diagnostics", boom)
    data = di.investigate(db, target="system")
    assert data["reports"][0]["status"] == "unknown"
    assert data["reports"][0]["root_cause"] == "overview_failed"
    assert all(r["status"] != "healthy" for r in data["reports"])


def test_overview_failure_target_routes_to_system_investigation(db, monkeypatch, no_network):
    monkeypatch.setattr(diagnostics, "run_diagnostics", lambda db_: {
        "overall": "unknown", "error": "overview failed", "items": [],
    })
    monkeypatch.setattr(di, "run_diagnostics", diagnostics.run_diagnostics)

    data = di.investigate(db, target="diagnostics.overview")

    assert data["target"] == "system"
    assert data["reports"][0]["target"] == "system"
    assert data["reports"][0]["root_cause"] == "overview_failed"
    assert "未知的诊断目标" not in data["reports"][0]["summary"]


def test_overview_self_reports_unknown_when_initialization_fails(db, monkeypatch):
    def broken_settings():
        raise RuntimeError("settings failed")

    monkeypatch.setattr(diagnostics, "get_diagnostic_settings", broken_settings)
    overview = diagnostics.run_diagnostics(db)
    assert overview["overall"] == "unknown"
    assert overview.get("error")
    assert overview["items"], "failure must be visible as an item"


def test_overview_isolates_one_probe_failure(db, monkeypatch):
    def broken_lookup(db_):
        raise RuntimeError("peek failed")

    monkeypatch.setattr(diagnostics, "peek_email_config", broken_lookup)
    overview = diagnostics.run_diagnostics(db)

    assert overview["overall"] != "unknown"
    assert overview.get("error") is None
    assert overview.get("trace_id")
    ai_item = next(item for item in overview["items"] if item["id"] == "ai.config")
    assert ai_item["status"] == "error"
    assert "RuntimeError" in ai_item["detail"]
    assert not any(item["id"] == "diagnostics.overview" for item in overview["items"])


def test_overview_normalizes_sqlite_naive_diagnostic_timestamps(db, monkeypatch, tmp_path):
    """A SQLite round-trip drops tzinfo but must not abort the whole overview."""
    monkeypatch.setattr(diagnostics, "is_gmail_configured", lambda s: True)
    now = datetime.now(timezone.utc)
    make_connected_account(db, expiry=now - timedelta(minutes=10))
    db.add(Automation(
        owner_id=1,
        name="diagnostic schedule",
        prompt="diagnostic only",
        plan_json="{}",
        status="enabled",
        next_run_at=now - timedelta(minutes=10),
    ))
    db.commit()

    heartbeat = tmp_path / "scheduler-heartbeat.json"
    heartbeat.write_text(
        json.dumps({"tick_at": (now - timedelta(seconds=10)).replace(tzinfo=None).isoformat()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(diagnostics._DATA, "queue", tmp_path)
    db.expire_all()  # Reload DateTime values through SQLite, without tzinfo.

    overview = diagnostics.run_diagnostics(db)

    assert overview["overall"] != "unknown"
    assert overview.get("error") is None
    assert not any(item["id"] == "diagnostics.overview" for item in overview["items"])
    gmail_items = [item for item in overview["items"] if item["id"] == "gmail.oauth"]
    assert len(gmail_items) == 1
    assert gmail_items[0]["status"] == "info"
    assert any(item["id"] == "automations.stuck" and item["status"] == "warn" for item in overview["items"])


# ---------------------------------------------------------------------------
# 4. Read-only guarantee: no directory creation, no write probes
# ---------------------------------------------------------------------------
def test_no_write_probe_in_sources():
    for module in (diagnostics, di):
        src = Path(module.__file__).read_text(encoding="utf-8")
        assert "write_text(\"ok\")" not in src
        assert ".write_text(" not in src
        assert "mkdir(parents=True" not in src
        assert "unlink()" not in src


def test_legacy_packaging_diagnose_writability_hint_creates_no_files(tmp_path):
    """The compatibility endpoint must not resurrect the old write probe."""
    missing = tmp_path / "does-not-exist"
    before = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")}
    assert system_api._is_writable(str(missing)) is False
    assert system_api._is_writable(str(tmp_path)) in {True, False}
    after = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")}
    assert before == after


def test_investigation_creates_no_files(db, tmp_path, monkeypatch):
    for attr in ("config", "database", "queue", "logs", "tacwork"):
        d = tmp_path / attr
        d.mkdir()
        monkeypatch.setattr(_DATA, attr, d)
    monkeypatch.setattr(_DATA, "root", tmp_path)
    monkeypatch.setattr(di, "_LOGS", tmp_path / "logs")
    (tmp_path / "config" / ".env").write_text("", encoding="utf-8")

    before = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")}
    di.investigate(db, target="system")
    diagnostics.run_diagnostics(db)
    after = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")}
    assert before == after


# ---------------------------------------------------------------------------
# 5. Redaction
# ---------------------------------------------------------------------------
def test_redact_text_masks_secrets_pii_and_paths():
    out = redact.redact_text(
        "Bearer abc12345 api_key=xyz9876 client_secret: q9q9q9 refresh_token: r8r8r8 "
        "ya29.abcdefghijk https://accounts.google.com/o/oauth2/token?code=CODE123&state=ST456 "
        "mail pyx1171898390@gmail.com dir C:\\Users\\Administrats\\Data root F:\\Project\\Email Automation"
    )
    for secret in ("abc12345", "xyz9876", "q9q9q9", "r8r8r8", "abcdefghijk", "CODE123", "ST456", "pyx1171898390", "Administrats"):
        assert secret not in out
    assert "***REDACTED***" in out
    assert "ya29.***REDACTED***" in out
    assert "***@gmail.com" in out
    assert "<path>" in out


def test_gmail_report_masks_email(db, monkeypatch, no_network):
    make_connected_account(db)
    monkeypatch.setattr(di, "is_gmail_configured", lambda s: True)
    data = di.investigate(db, target="gmail.oauth")
    blob = json.dumps(data, ensure_ascii=False)
    assert "pyx1171898390" not in blob
    assert "pyx***@gmail.com" in blob


def test_empty_allowlist_is_supported_configuration(db, monkeypatch, no_network):
    make_connected_account(db)  # connected + oauth => real_send derivable True
    monkeypatch.setattr(di, "is_real_send_enabled", lambda s, a, o: True)
    report = di.investigate(db, target="sending.safety")["reports"][0]
    blob = json.dumps(report, ensure_ascii=False)
    assert report["status"] == "healthy"
    assert "TEST_RECIPIENT_ALLOWLIST" not in blob
    assert any("0 address(es)" in e["finding"] for e in report["evidence"])


# ---------------------------------------------------------------------------
# 6. Gmail state machine (config-only checks, no refresh)
# ---------------------------------------------------------------------------
def test_gmail_states(db, monkeypatch, no_network):
    monkeypatch.setattr(di, "is_gmail_configured", lambda s: True)
    now = datetime.now(timezone.utc)

    # An expired access token plus a refresh token is still an operational
    # Gmail connection.  Diagnostics are read-only and must not falsely ask
    # for reauthorization.
    make_connected_account(db, expiry=now - timedelta(seconds=120), refresh=True)
    rep = di.investigate(db, target="gmail.oauth")["reports"][0]
    assert rep["status"] == "healthy"
    assert rep["root_cause"] is None

    # expired + no refresh token => confirmed
    acc = db.query(GmailAccount).first()
    acc.oauth.refresh_token_enc = None
    db.commit()
    rep = di.investigate(db, target="gmail.oauth")["reports"][0]
    assert rep["status"] == "confirmed"
    assert rep["root_cause"] == "token_expired_no_refresh"


def test_gmail_cursor_invalid_and_healthy(db, monkeypatch, no_network):
    monkeypatch.setattr(di, "is_gmail_configured", lambda s: True)
    now = datetime.now(timezone.utc)

    make_connected_account(db, history_id="1000", expiry=now + timedelta(hours=1))
    rep = di.investigate(db, target="gmail.oauth")["reports"][0]
    assert rep["root_cause"] == "history_cursor_invalid"
    assert rep["status"] == "confirmed"

    acc = db.query(GmailAccount).first()
    acc.history_id = "98765"
    db.commit()
    rep = di.investigate(db, target="gmail.oauth")["reports"][0]
    assert rep["status"] == "healthy"


def test_gmail_never_refreshes_or_calls_gmail(db, monkeypatch, no_network):
    monkeypatch.setattr(di, "is_gmail_configured", lambda s: True)
    make_connected_account(db, expiry=datetime.now(timezone.utc) - timedelta(seconds=60))
    rep = di.investigate(db, target="gmail.oauth")["reports"][0]
    assert rep["status"] == "healthy"


def test_overview_reports_expired_refreshable_gmail_token_once(db, monkeypatch):
    monkeypatch.setattr(diagnostics, "is_gmail_configured", lambda s: True)
    make_connected_account(db, expiry=datetime.now(timezone.utc) - timedelta(seconds=60), refresh=True)

    overview = diagnostics.run_diagnostics(db)
    gmail_items = [item for item in overview["items"] if item["id"] == "gmail.oauth"]

    assert len(gmail_items) == 1
    assert gmail_items[0]["status"] == "info"
    assert "automatically" in gmail_items[0]["detail"]


# ---------------------------------------------------------------------------
# 7. Collector isolation + evidence cap
# ---------------------------------------------------------------------------
def test_single_collector_failure_isolated(db, monkeypatch, no_network):
    def broken(db_, s):
        raise RuntimeError("collector blew up")

    monkeypatch.setitem(di._COLLECTORS, "disk.space", broken)
    data = di.investigate(db, target="disk.space")
    assert data["reports"][0]["status"] == "unknown"

    other = di.investigate(db, target="runtime.writable")["reports"][0]
    assert other["status"] != "unknown"


def test_evidence_capped(db, monkeypatch):
    def big(db_, s):
        return di._report(
            "disk.space", "healthy", None, None, "high", "ok",
            [{"source": "x", "finding": str(i)} for i in range(100)], [], [],
        )

    monkeypatch.setitem(di._COLLECTORS, "disk.space", big)
    report = di.investigate(db, target="disk.space")["reports"][0]
    assert len(report["evidence"]) == di._MAX_EVIDENCE


# ---------------------------------------------------------------------------
# 8. Trace id scoping
# ---------------------------------------------------------------------------
def test_trace_id_only_matches_own_lines(db, tmp_path, monkeypatch, no_network):
    logs = tmp_path
    (logs / "backend.log").write_text(
        "\n".join([
            "2026-09-07 10:00:00 [tid=abc] INFO request started",
            "2026-09-07 10:00:01 [tid=other] INFO unrelated line",
            "2026-09-07 10:00:02 [tid=abc] ERROR boom",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(di, "_LOGS", logs)

    data = di.investigate(db, target="gmail.oauth", trace_id="abc")
    blob = json.dumps(data["reports"][0]["evidence"], ensure_ascii=False)
    assert "tid=abc" in blob
    assert "tid=other" not in blob

    no_trace = di.investigate(db, target="gmail.oauth")
    assert not any(e["source"] == "backend.log" for e in no_trace["reports"][0]["evidence"])


# ---------------------------------------------------------------------------
# 9. Frontend / Electron stay manual (no polling, no on-mount fetch)
# ---------------------------------------------------------------------------
def test_frontend_has_no_polling_or_automount_fetch():
    root = Path(__file__).resolve().parents[2]
    tsx = (root / "frontend" / "components" / "Diagnostics.tsx").read_text(encoding="utf-8")
    assert "useEffect" not in tsx
    assert "setInterval" not in tsx

    main = (root / "desktop" / "main.cjs").read_text(encoding="utf-8")
    assert "DIAGNOSTICS_POLL" not in main
    assert "pollDiagnostics" not in main
    assert "setInterval" not in main


def test_frontend_rechecks_synthetic_overview_failure_instead_of_investigating_it():
    root = Path(__file__).resolve().parents[2]
    tsx = (root / "frontend" / "components" / "Diagnostics.tsx").read_text(encoding="utf-8")
    assert 'const OVERVIEW_FAILURE_ID = "diagnostics.overview"' in tsx
    assert "target === OVERVIEW_FAILURE_ID" in tsx
    assert "await runCheck()" in tsx


# ---------------------------------------------------------------------------
# 10. Electron-side redaction mirrors the backend (static check)
# ---------------------------------------------------------------------------
def test_electron_redaction_masks_pii():
    root = Path(__file__).resolve().parents[2]
    src = (root / "desktop" / "main.cjs").read_text(encoding="utf-8")
    for needle in ("Users|home|Documents and Settings", "code|state|access_token", "@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}"):
        assert needle in src, f"redaction rule missing: {needle}"
