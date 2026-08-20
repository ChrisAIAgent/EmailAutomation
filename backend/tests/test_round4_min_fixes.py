"""Round-4 minimal-fix regression tests.

Scope (per the user's instruction): only the two targeted hardening fixes,
no refactor / no new architecture:
  * RealGmailTransport.list_threads must re-raise GmailTimeoutError immediately
    and NOT keep fetching the remaining threads (which would each wait up to
    GMAIL_HTTP_TIMEOUT_SECONDS and could block for ~50x that long).
  * ChatOpenAI must be constructed with timeout=LLM_TIMEOUT_SECONDS and
    max_retries=1.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.agents.langgraph_agent import _make_llm
from app.gmail.client import RealGmailTransport
from app.gmail.transport import GmailTimeoutError


class _StubCipher:
    def decrypt(self, v):
        return v

    def encrypt(self, v):
        return v


# ---------------------------------------------------------------------------
# 1) list_threads re-raises GmailTimeoutError and does NOT request subsequent
#    threads. Prevents ~50 threads from each serially waiting on the timeout.
# ---------------------------------------------------------------------------
def test_list_threads_reraises_gmail_timeout_and_skips_remaining():
    class _T(RealGmailTransport):
        calls = 0

        def _call(self, fn):
            # The threads().list() call succeeds and returns 3 thread ids;
            # the per-thread get_thread() calls are what can time out.
            return {"threads": [{"id": "t1"}, {"id": "t2"}, {"id": "t3"}]}

        def get_thread(self, thread_id):
            _T.calls += 1
            raise GmailTimeoutError("gmail_timeout: simulated per-thread timeout")

    with patch("app.gmail.client.Cipher", _StubCipher):
        t = _T(account=object(), oauth_row=object())
    _T.calls = 0

    with pytest.raises(GmailTimeoutError):
        t.list_threads("query")

    assert _T.calls == 1, (
        "list_threads must re-raise on the FIRST thread timeout and not keep "
        f"fetching remaining threads; get_thread was called {_T.calls} time(s)"
    )


# ---------------------------------------------------------------------------
# 2) ChatOpenAI is constructed with timeout=LLM_TIMEOUT_SECONDS and max_retries=1.
# ---------------------------------------------------------------------------
def test_chat_openai_has_timeout_and_max_retries(monkeypatch):
    captured = []

    class _FakeSettings:
        effective_llm_api_key = "sk-test"
        effective_llm_model = "gpt-4o-mini"
        LLM_PROVIDER = "openai"
        LLM_BASE_URL = None
        LLM_API_MODE = "chat_completions"
        LLM_TIMEOUT_SECONDS = 30

    monkeypatch.setattr(
        "app.agents.langgraph_agent.get_settings", lambda: _FakeSettings()
    )
    # Patch ChatOpenAI at its source module so the local import inside
    # _make_llm picks up the double and records the kwargs it was called with.
    import langchain_openai

    monkeypatch.setattr(
        langchain_openai, "ChatOpenAI",
        lambda **kw: captured.append(kw) or kw,
    )

    result = _make_llm()

    assert result is not None, "LLM must be constructed when configured"
    kw = captured[0]
    assert kw.get("timeout") == 30, f"ChatOpenAI must get timeout=30, got {kw.get('timeout')!r}"
    assert kw.get("max_retries") == 1, f"ChatOpenAI must get max_retries=1, got {kw.get('max_retries')!r}"
    assert kw.get("temperature") == 0

    _make_llm(task="reply")
    assert captured[1]["temperature"] == 0.35
