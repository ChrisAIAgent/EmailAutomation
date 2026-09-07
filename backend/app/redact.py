"""Shared redaction helpers for diagnostic reports.

Every diagnostic surface (overview, root-cause investigation) must render
evidence through these helpers before it reaches a user or a report file.

- Never emit tokens, API keys, secrets, or OAuth URL parameters.
- Mask email addresses, Windows/POSIX user directories, and absolute paths.
- Keep the finding bounded in length.

Redaction is deliberately conservative: when in doubt, mask.
"""
from __future__ import annotations

import re
from typing import Any, Optional, Sequence

MAX_FINDING_CHARS = 300

_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)((?:api[_-]?key|apikey)\s*[:=]\s*)[^\s,'\"]+"),
    re.compile(r"(?i)((?:client[_-]?secret|secret)\s*[:=]\s*)[^\s,'\"]+"),
    re.compile(r"(?i)((?:access|refresh|id)[_-]?token\s*[:=]\s*)[^\s,'\"]+"),
    re.compile(r"(?i)(ya29\.)[A-Za-z0-9._\-]+"),
]

# code= / state= / access_token= ... inside URLs (OAuth round-trips).
_OAUTH_URL_PARAM = re.compile(
    r"(?i)([?&](?:code|state|access_token|refresh_token|id_token|token|client_secret)=)[^&\s'\"]+"
)

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# C:\Users\<name> / /Users/<name> / /home/<name> -> keep prefix, mask name.
_USER_DIR = re.compile(
    r"(?i)((?:[A-Za-z]:)?[\\/]*(?:Users|home|Documents and Settings)[\\/]+)([^\s,'\"\\/]+)"
)

# Any remaining Windows drive path or absolute POSIX path. Separator runs of
# one or two slashes are allowed because JSON escaping doubles backslashes.
_WIN_PATH = re.compile(r"[A-Za-z]:[\\/]+(?:[\w.\- ]+[\\/]+)*[\w.\- ]+")
_POSIX_PATH = re.compile(r"/(?:usr|etc|var|opt|tmp|home|root|Users)(?:/[\w.\-]+)+")


def mask_email(value: Any) -> str:
    """``pyx1171898390@gmail.com`` -> ``pyx***@gmail.com``; empty-safe."""
    text = str(value or "").strip()
    if "@" not in text:
        return text
    local, _, domain = text.partition("@")
    head = local[:3] if len(local) > 3 else local
    return f"{head}***@{domain}"


def redact_text(value: Any, safe_roots: Optional[Sequence[str]] = None) -> str:
    """Return a bounded, redacted rendering of ``value`` for reports."""
    out = str(value if value is not None else "")

    # 1. Well-known roots keep their meaning but lose their location. Both the
    #    raw form and the JSON-escaped form (doubled backslashes) are replaced,
    #    because evidence often embeds paths inside JSON.stringify output.
    for i, root in enumerate(safe_roots or []):
        if not root:
            continue
        out = out.replace(root, f"<root{i}>")
        escaped = root.replace("\\", "\\\\")
        if escaped != root:
            out = out.replace(escaped, f"<root{i}>")

    # 2. OAuth URL parameters, then generic secrets.
    out = _OAUTH_URL_PARAM.sub(lambda m: m.group(1) + "***REDACTED***", out)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: m.group(1) + "***REDACTED***", out)

    # 3. PII and filesystem layout (user dirs before generic paths).
    out = _EMAIL.sub(lambda m: mask_email(m.group(0)), out)
    out = _USER_DIR.sub(lambda m: m.group(1) + "***", out)
    out = _WIN_PATH.sub("<path>", out)
    out = _POSIX_PATH.sub("<path>", out)

    return out[:MAX_FINDING_CHARS]
