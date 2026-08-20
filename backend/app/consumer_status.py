"""File-backed Huey consumer status shared with the API health endpoint."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


from .config import _DATA

# Backwards-compatible alias for the legacy data dir (backend/data). Kept so
# existing tests that monkeypatch ``consumer_status.DATA_DIR`` keep working.
DATA_DIR = _DATA.queue
STATUS_PATH = _DATA.queue / "consumer-status.json"


def write_consumer_status(state: str = "running", error: str | None = None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": state,
        "pid": os.getpid(),
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
    }
    temp_path = STATUS_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload), encoding="utf-8")
    temp_path.replace(STATUS_PATH)


def read_consumer_status(max_age_seconds: int = 150) -> dict:
    if not STATUS_PATH.exists():
        return {
            "healthy": False,
            "state": "missing",
            "pid": None,
            "heartbeat_at": None,
            "age_seconds": None,
        }
    try:
        payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        heartbeat = datetime.fromisoformat(payload["heartbeat_at"])
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        age = max(0, int((datetime.now(timezone.utc) - heartbeat).total_seconds()))
        state = payload.get("state") or "unknown"
        return {
            **payload,
            "healthy": state == "running" and age <= max_age_seconds,
            "age_seconds": age,
        }
    except Exception as exc:
        return {
            "healthy": False,
            "state": "invalid",
            "pid": None,
            "heartbeat_at": None,
            "age_seconds": None,
            "error": str(exc)[:300],
        }
