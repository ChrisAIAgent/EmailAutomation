"""Application configuration loaded from environment / .env file."""
from __future__ import annotations

import json
import os
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Data directory resolution (runbook Section 4) -------------------------
# The program directory may be read-only (e.g. installed under
# C:\Program Files). In that case all mutable data (DB, queue, logs, .env,
# keys, TACWork session) must live under the user's LOCALAPPDATA. When running
# from a writable location (dev machine, portable zip), we keep the legacy
# local layout so existing behaviour and tests are unaffected.
# EMAIL_AUTOMATION_DATA_DIR overrides everything (used by launchers/tests).
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_protected_install() -> bool:
    """True when the backend lives somewhere we cannot safely write to."""
    lowered = _BACKEND_DIR.lower().replace("/", "\\")
    if "program files" in lowered:
        return True
    try:
        return not os.access(_BACKEND_DIR, os.W_OK)
    except OSError:
        return True


def resolve_data_dir():
    """Return the data-directory layout for the current deployment.

    Returns a namespace with ``.mode`` ("app" or "legacy") and the subdirs
    ``config``, ``database``, ``queue``, ``logs``, ``tacwork`` as Path objects.
    All subdirs are created (idempotent).
    """
    env = os.environ.get("EMAIL_AUTOMATION_DATA_DIR")
    if env:
        root = Path(env)
        mode = "app"
    elif _is_protected_install():
        local = (
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or tempfile.gettempdir()
        )
        root = Path(local) / "TAC AISolution" / "Email Automation"
        mode = "app"
    else:
        root = Path(_BACKEND_DIR)
        mode = "legacy"

    if mode == "app":
        sub = {
            "config": root / "config",
            "database": root / "database",
            "queue": root / "queue",
            "logs": root / "logs",
            "tacwork": root / "tacwork",
        }
    else:
        sub = {
            "config": root,
            "database": root,
            "queue": root / "data",
            "logs": root / "logs",
            "tacwork": root / "data" / "tacwork",
        }
    for p in sub.values():
        p.mkdir(parents=True, exist_ok=True)

    # Legacy data migration is intentionally explicit; a fresh app-mode directory
    # must never inherit business data from the source/install tree.
    return type("DataDir", (), {**sub, "mode": mode, "root": root})()


# Resolved once at import; other modules import ``_DATA`` for stable paths.
_DATA = resolve_data_dir()
_DEFAULT_SQLITE = f"sqlite:///{_DATA.database / 'app.db'}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_DATA.config / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core ---
    APP_NAME: str = "Gmail Outreach Agent"
    APP_ENV: str = "development"
    APP_URL: str = "http://127.0.0.1:18001"
    API_URL: str = "http://127.0.0.1:18000"
    # Database URL. Local default uses an absolute SQLite path under backend/;
    # provide postgresql://... (or sqlite:///...) via env for prod.
    DATABASE_URL: str = _DEFAULT_SQLITE
    APP_ENCRYPTION_KEY: Optional[str] = None  # Fernet key (32 url-safe b64 bytes)

    # --- Google OAuth ---
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None
    GOOGLE_REDIRECT_URI: str = "http://127.0.0.1:18000/api/gmail/oauth/callback"

    # --- LLM (LangGraph) ---
    LLM_PROVIDER: str = "openai"
    LLM_API_KEY: Optional[str] = None
    LLM_BASE_URL: Optional[str] = None
    LLM_MODEL: Optional[str] = None
    LLM_API_MODE: str = "chat_completions"  # chat_completions | responses
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4o-mini"
    # LLM client socket timeout (seconds) applied to ChatOpenAI. Without this the
    # LLM call can block indefinitely on a slow/hung provider. Default 30s.
    LLM_TIMEOUT_SECONDS: int = 30

    @property
    def effective_llm_api_key(self) -> Optional[str]:
        return self.LLM_API_KEY or self.OPENAI_API_KEY

    @property
    def effective_llm_model(self) -> str:
        return self.LLM_MODEL or self.OPENAI_MODEL

    # --- OpenClaw adapter ---
    OPENCLAW_TRANSPORT: str = "http"  # http | webhook | cli | mcp
    OPENCLAW_ENDPOINT: Optional[str] = None
    OPENCLAW_COMMAND: Optional[str] = None
    OPENCLAW_API_KEY: Optional[str] = None
    OPENCLAW_TIMEOUT_SECONDS: int = 30

    # --- Automation webhook (OpenClaw cron calls POST /api/automation/tick) ---
    # If set, the tick endpoint requires `Authorization: Bearer <this token>`.
    # OpenClaw NEVER receives a Gmail token and NEVER calls Gmail directly.
    AUTOMATION_WEBHOOK_TOKEN: Optional[str] = None

    # --- Safety ---
    RESTRICTED_RECIPIENT_ALLOWLIST: str = ""  # comma-separated emails
    # Legacy deployment compatibility only. New configuration must use
    # RESTRICTED_RECIPIENT_ALLOWLIST; an existing .env keeps working unchanged.
    TEST_RECIPIENT_ALLOWLIST: Optional[str] = None
    # Operator-owned addresses that must NEVER be auto-managed as customers
    # (auto-stopped / auto-statused by the Agent). Defaults to empty; the
    # real-send allowlist is always included via `is_internal_test_email`.
    INTERNAL_TEST_EMAILS: str = ""
    DEFAULT_TIMEZONE: str = "Asia/Shanghai"
    ENABLE_REAL_SEND: bool = False  # MUST default to False
    ALLOW_INMEMORY_GMAIL: bool = False  # unit tests / explicit dev injection only

    # --- Gmail network timeout ---
    # Hard socket timeout (seconds) applied to EVERY real Gmail API request and
    # to the OAuth token-refresh POST. Without this, httplib2 blocks forever on
    # an unreachable/slow Gmail endpoint and the worker thread never returns
    # (the run would wedge in `running`). Default 25s (within 20-30s range).
    GMAIL_HTTP_TIMEOUT_SECONDS: int = 25

    # --- Scheduler ---
    ENABLE_SCHEDULER: bool = True
    POLL_INTERVAL_SECONDS: int = 60
    # A run stuck in `running` longer than this is reclaimed as `failed`
    # (reason `worker_timeout`) so a crashed consumer or an unreachable external
    # dependency cannot wedge an Automation forever.
    STALE_RUN_TIMEOUT_MINUTES: int = 15

    # --- Embedded TACWork takeover scheduler ---
    # Loopback-only TACWork server used by the unified desktop runtime.  The
    # fixed client token is the same local collaboration token passed by
    # scripts/tacwork-runtime.ps1; deployments may override both values.
    TACWORK_SERVER_URL: str = "http://127.0.0.1:18002"
    TACWORK_CLIENT_TOKEN: str = "email-automation-local-v1"
    TACWORK_HTTP_TIMEOUT_SECONDS: int = 15

    # --- App ---
    SECRET_KEY: str = "dev-insecure-secret-change-me"
    CORS_ORIGINS: str = "app://email-automation,http://127.0.0.1:18001,http://localhost:18001"

    @property
    def recipient_allowlist(self) -> set[str]:
        return {
            e.strip().lower()
            for e in (
                self.RESTRICTED_RECIPIENT_ALLOWLIST
                or self.TEST_RECIPIENT_ALLOWLIST
                or ""
            ).split(",")
            if e.strip()
        }

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in (self.CORS_ORIGINS or "").split(",") if o.strip()]


def _load_or_create_security(config_dir: Path) -> dict:
    """Load or persist encryption/secret keys (runbook Section 5.7).

    In legacy mode we never persist (keeps dev/tests side-effect free); the
    in-memory defaults from Settings are used. In Windows app mode the keys are
    protected in the same current-user DPAPI store as OAuth configuration.
    A legacy ``security.json`` is imported once but never deleted automatically.
    Lost/unreadable DPAPI credentials are reported rather than silently rotated.
    """
    if _DATA.mode == "legacy":
        return {
            "encryption_key": os.environ.get("APP_ENCRYPTION_KEY"),
            "secret_key": os.environ.get("SECRET_KEY"),
        }
    from . import credential_store
    stored = credential_store.load(Path(config_dir))
    protected = stored.get("runtime_security")
    if isinstance(protected, dict) and protected.get("encryption_key") and protected.get("secret_key"):
        return protected
    legacy_path = Path(config_dir) / "security.json"
    if legacy_path.exists():
        try:
            data = json.loads(legacy_path.read_text(encoding="utf-8"))
            if data.get("encryption_key") and data.get("secret_key"):
                credential_store.update(Path(config_dir), {"runtime_security": data})
                return data
        except (ValueError, OSError):
            pass
    import secrets as _secrets

    data = {
        "encryption_key": os.environ.get("APP_ENCRYPTION_KEY")
        or _secrets.token_urlsafe(32),
        "secret_key": os.environ.get("SECRET_KEY") or _secrets.token_urlsafe(32),
    }
    credential_store.update(Path(config_dir), {"runtime_security": data})
    return data


def _load_desktop_oauth_into(s: Settings) -> None:
    """Apply Desktop OAuth configuration without creating local credentials."""
    # Installed runtimes read customer-owned Desktop OAuth configuration from
    # the same current-user DPAPI store used by Backend and Consumer.
    try:
        from .oauth_config import load as load_desktop_oauth, redirect_uri
        desktop_oauth = load_desktop_oauth(_DATA.config)
        if desktop_oauth:
            s.GOOGLE_CLIENT_ID = desktop_oauth.get("client_id")
            s.GOOGLE_CLIENT_SECRET = desktop_oauth.get("client_secret")
            s.GOOGLE_REDIRECT_URI = redirect_uri(s.API_URL)
    except RuntimeError:
        # Status endpoints surface credential_key_unavailable; application import
        # remains available so the user can repair or re-authorize.
        pass


def get_diagnostic_settings() -> Settings:
    """Return a configuration snapshot suitable for strictly read-only probes.

    Normal startup intentionally initializes the per-user DPAPI security store
    when it is absent. Manual diagnostics must report that state without
    creating ``credentials.dat`` merely because an operator opened a report.
    """
    s = Settings()
    _load_desktop_oauth_into(s)
    return s


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    _load_desktop_oauth_into(s)
    keys = _load_or_create_security(_DATA.config)
    if not s.APP_ENCRYPTION_KEY and keys.get("encryption_key"):
        s.APP_ENCRYPTION_KEY = keys["encryption_key"]
    if (
        not s.SECRET_KEY or s.SECRET_KEY == "dev-insecure-secret-change-me"
    ) and keys.get("secret_key"):
        s.SECRET_KEY = keys["secret_key"]
    return s


def data_dir_info() -> dict:
    """Expose the resolved data-directory layout for diagnostics."""
    return {
        "mode": _DATA.mode,
        "root": str(_DATA.root),
        "config": str(_DATA.config),
        "database": str(_DATA.database),
        "queue": str(_DATA.queue),
        "logs": str(_DATA.logs),
        "tacwork": str(_DATA.tacwork),
        "env_override": bool(os.environ.get("EMAIL_AUTOMATION_DATA_DIR")),
    }


def is_gmail_configured(s: Optional[Settings] = None) -> bool:
    s = s or get_settings()
    return bool(s.GOOGLE_CLIENT_ID and s.GOOGLE_CLIENT_SECRET)


def is_llm_configured(s: Optional[Settings] = None) -> bool:
    s = s or get_settings()
    return bool(s.effective_llm_api_key and s.effective_llm_model)


def is_openclaw_configured(s: Optional[Settings] = None) -> bool:
    s = s or get_settings()
    if s.OPENCLAW_TRANSPORT == "cli":
        return bool(s.OPENCLAW_COMMAND)
    return bool(s.OPENCLAW_ENDPOINT)


def is_internal_test_email(email: Optional[str]) -> bool:
    """True if `email` belongs to the operator rather than a real customer.

    The automation must never auto-stop or auto-status these addresses as if
    they were leads: a misclassification must not flip the operator's own
    mailbox (used for real-send testing / seeded as a demo contact) into
    "unsubscribed" / "stopped". Covers both an explicit ``INTERNAL_TEST_EMAILS``
    list and the real-send ``TEST_RECIPIENT_ALLOWLIST`` (which is, by
    definition, the operator's own address(es))."""
    if not email:
        return False
    s = get_settings()
    allowed = {
        e.strip().lower()
        for e in (s.INTERNAL_TEST_EMAILS or "").split(",")
        if e.strip()
    }
    allowed |= s.recipient_allowlist
    return email.strip().lower() in allowed
