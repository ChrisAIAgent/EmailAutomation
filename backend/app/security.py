"""Security helpers: symmetric encryption for tokens/keys, idempotency keys."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings, get_settings


def _derive_fernet_key(raw: str) -> bytes:
    """Derive a 32-byte url-safe base64 Fernet key from an arbitrary secret string."""
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class Cipher:
    """Symmetric encryption for sensitive fields (OAuth tokens, model keys)."""

    def __init__(self, settings: Settings | None = None):
        settings = settings or get_settings()
        raw = settings.APP_ENCRYPTION_KEY or settings.SECRET_KEY
        self._f = Fernet(_derive_fernet_key(raw))

    def encrypt(self, plaintext: str) -> str:
        if plaintext is None:
            return None  # type: ignore
        return self._f.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            return self._f.decrypt(token.encode("utf-8")).decode("utf-8")
        except (InvalidToken, Exception):
            return None


_cipher: Cipher | None = None


def get_cipher() -> Cipher:
    global _cipher
    if _cipher is None:
        _cipher = Cipher()
    return _cipher


def new_idempotency_key(namespace: str, *parts: str) -> str:
    """Deterministic, collision-safe idempotency key.

    Same inputs always produce the same key, so retries/refreshes/callbacks
    are de-duplicated at the execution layer.
    """
    material = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{namespace}:{digest}"


def new_run_id() -> str:
    return "run_" + secrets.token_hex(16)


def safe_hmac(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def redact_for_log(value: Any, keep: int = 6) -> str:
    """Redact a token/secret for logging."""
    s = str(value)
    if len(s) <= keep * 2:
        return "***"
    return s[:keep] + "..." + s[-keep:]


def redact_body(text: str | None, limit: int = 80) -> str | None:
    """Truncate email body for logging (no full bodies in logs)."""
    if not text:
        return text
    text = text.replace("\n", " ")
    if len(text) > limit:
        return text[:limit] + "...(truncated)"
    return text


def json_safe(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
