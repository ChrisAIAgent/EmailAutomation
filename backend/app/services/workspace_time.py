"""UTC storage plus one persisted, human-facing Workspace time zone."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import flags

FLAG_DISPLAY_TIMEZONE = "workspace_display_timezone"
DEFAULT_TIMEZONE = "UTC"


def validate_timezone(value: str | None) -> str:
    candidate = (value or "").strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("invalid_workspace_display_timezone") from exc
    return candidate


def get_timezone(db) -> str:
    stored = flags.get_flag(db, FLAG_DISPLAY_TIMEZONE, DEFAULT_TIMEZONE)
    try:
        return validate_timezone(stored)
    except ValueError:
        return DEFAULT_TIMEZONE


def set_timezone(db, value: str) -> str:
    zone = validate_timezone(value)
    flags.set_flag(db, FLAG_DISPLAY_TIMEZONE, zone)
    return zone


def as_utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    # SQLite returns naive values; all persisted schedule timestamps are UTC.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_iso(value: datetime | str | None) -> str | None:
    parsed = as_utc(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def display(value: datetime | str | None, zone: str) -> dict[str, str | None]:
    parsed = as_utc(value)
    if not parsed:
        return {"utc": None, "local": None}
    local = parsed.astimezone(ZoneInfo(zone))
    offset = local.strftime("%z")
    offset = f"GMT{offset[:3]}:{offset[3:]}" if offset else "GMT"
    return {
        "utc": utc_iso(parsed),
        "local": f"{local.strftime('%Y-%m-%d %H:%M:%S')} {offset} ({zone})",
    }
