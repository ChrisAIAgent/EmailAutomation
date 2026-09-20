"""Per-user credential storage for Windows DPAPI and macOS Keychain."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


_UI_FORBIDDEN = 0x1
_MAC_KEYCHAIN_SERVICE = "com.tacaisolution.email-automation.credentials"


def _credential_path(config_dir: Path) -> Path:
    return Path(config_dir) / "credentials.dat"


def _dpapi(raw: bytes, *, protect: bool) -> bytes:
    """Protect/unprotect bytes with DPAPI for the current Windows user."""
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is unavailable")

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    source = ctypes.create_string_buffer(raw)
    source_blob = DATA_BLOB(len(raw), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    target_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    ok = (
        crypt32.CryptProtectData(
            ctypes.byref(source_blob), None, None, None, None, _UI_FORBIDDEN, ctypes.byref(target_blob)
        )
        if protect
        else crypt32.CryptUnprotectData(
            ctypes.byref(source_blob), None, None, None, None, _UI_FORBIDDEN, ctypes.byref(target_blob)
        )
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target_blob.pbData, target_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target_blob.pbData)


def _mac_account(config_dir: Path) -> str:
    digest = hashlib.sha256(str(Path(config_dir).expanduser().resolve()).encode("utf-8")).hexdigest()[:20]
    return f"email-automation-{digest}"


def _mac_read(config_dir: Path) -> dict:
    result = subprocess.run(
        [
            "/usr/bin/security", "find-generic-password",
            "-a", _mac_account(config_dir),
            "-s", _MAC_KEYCHAIN_SERVICE,
            "-w",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # `security` uses a non-zero result when the item does not exist. Do
        # not include stderr in an exception because it can expose Keychain
        # metadata from the current user profile.
        return {}
    try:
        decoded = base64.b64decode(result.stdout.strip().encode("ascii"), validate=True)
        value = json.loads(decoded.decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception as exc:
        raise RuntimeError("credential_key_unavailable") from exc


def _mac_write(config_dir: Path, values: dict) -> None:
    encoded = base64.b64encode(
        json.dumps(values, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    result = subprocess.run(
        [
            "/usr/bin/security", "add-generic-password",
            "-a", _mac_account(config_dir),
            "-s", _MAC_KEYCHAIN_SERVICE,
            "-w", encoded,
            "-U",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("credential_key_unavailable")


def load(config_dir: Path) -> dict:
    """Load credentials without logging or exposing their contents."""
    if sys.platform == "darwin":
        return _mac_read(config_dir)
    path = _credential_path(config_dir)
    if not path.exists():
        return {}
    try:
        raw = base64.b64decode(path.read_bytes(), validate=True)
        result = json.loads(_dpapi(raw, protect=False).decode("utf-8"))
        return result if isinstance(result, dict) else {}
    except Exception as exc:
        raise RuntimeError("credential_key_unavailable") from exc


def save(config_dir: Path, values: dict) -> None:
    """Atomically persist a DPAPI-protected JSON object."""
    if sys.platform == "darwin":
        _mac_write(config_dir, values)
        return
    if os.name != "nt":
        raise RuntimeError("credential_key_unavailable")
    path = _credential_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    protected = _dpapi(json.dumps(values, ensure_ascii=False).encode("utf-8"), protect=True)
    temp = path.with_suffix(".tmp")
    temp.write_bytes(base64.b64encode(protected))
    os.replace(temp, path)


def update(config_dir: Path, values: dict) -> dict:
    current = load(config_dir)
    current.update({key: value for key, value in values.items() if value is not None})
    save(config_dir, current)
    return current


def exists(config_dir: Path) -> bool:
    if sys.platform == "darwin":
        return bool(_mac_read(config_dir))
    return _credential_path(config_dir).exists()
