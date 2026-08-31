"""Agent health + global pause + restart control + packaging diagnostics."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import models
from ..agents.orchestrator import Orchestrator
from ..config import (
    _BACKEND_DIR,
    _DATA,
    data_dir_info,
    get_settings,
    is_gmail_configured,
    is_llm_configured,
)
from ..services import flags as flag_svc
from ..services.ai_config import peek_email_config
from .deps import get_db

router = APIRouter(tags=["agent", "system"])


@router.get("/api/agent/health")
def agent_health(db: Session = Depends(get_db)):
    return [h.model_dump() for h in Orchestrator(db).health()]


def _is_writable(path: str) -> bool:
    """Best-effort writability probe for a directory (runbook Section 8)."""
    try:
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".ea_write_test"
        probe.write_text("ok")
        probe.unlink()
        return True
    except Exception:
        return False


@router.get("/api/system/diagnose")
def diagnose(db: Session = Depends(get_db)):
    """Packaging diagnostics (runbook Section 5/8/10).

    Returns backend-resolvable facts plus the set of runbook error codes that
    currently apply. The PowerShell ``portable-diagnose.ps1`` covers the
    runtime-side checks (x64 / VC++ / ports / wheel integrity) that the backend
    cannot observe directly.
    """
    s = get_settings()
    info = data_dir_info()

    backend_dir = Path(_BACKEND_DIR)
    project_root = backend_dir.parent
    env_file = Path(str(_DATA.config / ".env"))
    venv_complete = (backend_dir / ".venv" / "Scripts" / "python.exe").exists()
    node_modules_complete = (
        project_root / "frontend" / "node_modules" / ".bin" / "next.cmd"
    ).exists()

    # Node runtime version (bundled), if present.
    node_version = None
    node_exe = project_root / "tools" / "node" / "node.exe"
    if node_exe.exists():
        try:
            out = subprocess.run(
                [str(node_exe), "--version"],
                capture_output=True, text=True, timeout=5,
            )
            node_version = (out.stdout or out.stderr).strip()
        except Exception:
            node_version = None

    # DB reachability + gmail/llm status.
    db_ok = False
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    gmail_cfg = is_gmail_configured(s)
    gmail_conn = bool(
        db.query(models.GmailAccount)
        .filter(models.GmailAccount.is_connected == True, models.GmailAccount.oauth != None)  # noqa: E711
        .first()
    ) if db_ok else False
    llm_cfg = bool(peek_email_config(db)) or is_llm_configured(s)

    # Version consistency (runbook Section 5.6 / stale_runtime): the installer
    # stamps payload/version.txt; it must match frontend/package.json.
    version_mismatch = False
    installed_version = None
    frontend_version = None
    try:
        vt = (project_root / "version.txt").read_text(encoding="utf-8").strip()
        pkg = json.loads(
            (project_root / "frontend" / "package.json").read_text(encoding="utf-8")
        )
        installed_version = vt
        frontend_version = pkg.get("version")
        if vt and frontend_version and vt != frontend_version:
            version_mismatch = True
    except Exception:
        version_mismatch = False

    # Compose the runbook error-code set that currently applies.
    error_codes = []
    if not venv_complete:
        error_codes.append("runtime_missing")
    if not _is_writable(info["root"]):
        error_codes.append("permission_denied")
    if not db_ok:
        error_codes.append("database_migration_failed")
    if not llm_cfg:
        error_codes.append("ai_not_configured")
    if gmail_cfg and not gmail_conn:
        error_codes.append("oauth_not_connected")
    elif not gmail_cfg:
        error_codes.append("oauth_not_configured")
    if version_mismatch:
        error_codes.append("stale_runtime")

    return {
        "python_version": f"{sys.version.split()[0]}",
        "node_version": node_version,
        "installed_version": installed_version,
        "frontend_version": frontend_version,
        "data_dir": info,
        "data_dir_writable": _is_writable(info["root"]),
        "database_writable": _is_writable(info["database"]),
        "env_file_exists": env_file.exists(),
        "venv_complete": venv_complete,
        "node_modules_complete": node_modules_complete,
        "gmail_configured": gmail_cfg,
        "gmail_connected": gmail_conn,
        "llm_configured": llm_cfg,
        "cors_origins": s.cors_origin_list,
        "error_codes": error_codes,
    }


@router.get("/api/system/pause")
def get_pause(db: Session = Depends(get_db)):
    return {"global_pause": flag_svc.is_globally_paused(db)}


@router.post("/api/system/pause")
def set_pause(paused: bool, db: Session = Depends(get_db)):
    flag_svc.set_global_pause(db, paused)
    db.commit()
    return {"global_pause": paused}


def _project_root() -> Path:
    """Absolute project root (parent of backend/), derived from this file.

    The backend process CWD is ``backend/``, so a relative marker path would be
    resolved under ``backend/logs/run/`` while the watcher looks under the
    project root. Anchoring to ``__file__`` keeps both sides consistent.
    """
    return Path(__file__).resolve().parent.parent.parent.parent


def _restart_marker() -> Path:
    # Restart marker lives in the resolved data dir (runbook Section 4: logs/)
    # rather than the (possibly read-only) project root.
    from ..config import _DATA

    return _DATA.logs / "run" / "restart-requested.json"


@router.post("/api/system/restart")
def restart_services(force: bool = False, db: Session = Depends(get_db)):
    """Trigger a clean restart of the full stack via an on-demand watcher.

    The handler:
    1. Refuses when pending (sending/unknown) DeliveryAttempts exist, unless
       ``force=true``.
    2. Writes a marker file (audit + idempotency guard for the watcher).
    3. Spawns a detached watcher (``scripts/start-restart-watcher.ps1``) that
       performs ``stop-stack.ps1`` + ``start-stack.ps1`` once the backend exits.
    4. Exits the current backend process gracefully after 1 second.

    Returns immediately; the actual restart takes ~10-20s. Agents should poll
    ``/api/health`` until ``status == ok`` and ``consumer.healthy == true``.
    """
    if not force:
        pending = db.query(models.DeliveryAttempt).filter(
            models.DeliveryAttempt.status.in_(["sending", "unknown"])
        ).count()
        if pending > 0:
            raise HTTPException(
                409,
                detail=(
                    "cannot restart: pending DeliveryAttempts require "
                    "reconciliation first, or use force=true"
                ),
            )

    marker = _restart_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "reason": "agent_initiated",
        }),
        encoding="utf-8",
    )

    # Detached watcher: independent of this request thread, no console window.
    watcher = _project_root() / "scripts" / "start-restart-watcher.ps1"
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", str(watcher),
        "-Root", str(_project_root()),
    ]
    try:
        # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP keeps the watcher hidden
        # and detached from the dying backend console.
        subprocess.Popen(
            cmd,
            cwd=str(_project_root()),
            creationflags=0x08000000 | 0x00000200,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception:
        # Watcher spawn failure must not silently lose the request: surface it.
        marker.unlink(missing_ok=True)
        raise HTTPException(500, detail="failed to spawn restart watcher")

    def _delayed_exit():
        import time
        time.sleep(1)
        os._exit(0)

    threading.Thread(target=_delayed_exit, daemon=True).start()
    return {"status": "restart_scheduled", "message": "Services will restart in 1 second"}
