"""Customer-owned Google Desktop OAuth configuration in per-user DPAPI storage."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import credential_store

_ALLOWED_AUTH = "https://accounts.google.com/o/oauth2/auth"
_ALLOWED_TOKEN = "https://oauth2.googleapis.com/token"


def validate_desktop_config(payload: dict[str, Any]) -> dict[str, str]:
    if not isinstance(payload, dict) or "web" in payload:
        raise ValueError("desktop_oauth_required")
    installed = payload.get("installed")
    if not isinstance(installed, dict):
        raise ValueError("desktop_oauth_required")
    client_id = str(installed.get("client_id") or "").strip()
    client_secret = str(installed.get("client_secret") or "").strip()
    auth_uri = str(installed.get("auth_uri") or "").strip()
    token_uri = str(installed.get("token_uri") or "").strip()
    if len(client_id) < 10 or not client_id.endswith(".apps.googleusercontent.com"):
        raise ValueError("desktop_oauth_client_id_invalid")
    if len(client_secret) < 8:
        raise ValueError("desktop_oauth_client_secret_invalid")
    if auth_uri != _ALLOWED_AUTH or token_uri != _ALLOWED_TOKEN:
        raise ValueError("desktop_oauth_endpoint_invalid")
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": auth_uri,
        "token_uri": token_uri,
    }


def load(config_dir: Path) -> dict[str, str] | None:
    values = credential_store.load(config_dir)
    item = values.get("google_desktop_oauth")
    return item if isinstance(item, dict) else None


def save(config_dir: Path, payload: dict[str, Any]) -> dict[str, str]:
    normalized = validate_desktop_config(payload)
    credential_store.update(config_dir, {"google_desktop_oauth": normalized})
    return normalized


def redirect_uri(api_url: str) -> str:
    parsed = urlparse(api_url)
    port = parsed.port or 8000
    return f"http://127.0.0.1:{port}/api/gmail/oauth/callback"