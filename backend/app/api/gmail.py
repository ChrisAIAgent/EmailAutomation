"""Gmail OAuth + sync + disconnect endpoints."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from .. import models
from ..config import _DATA, get_settings, is_gmail_configured
from ..errors import ApiError
from ..events import publish
from ..gmail import auth, clear_transport_cache
from ..security import Cipher, redact_for_log
from ..services import sync as sync_svc
from .. import oauth_config as oauth_config_svc
from ..services.accounts import resolve_sending_account
from ..services.real_send import is_real_send_enabled
from .. import tasks
from .deps import get_db, ensure_owner

logger = logging.getLogger("api.gmail")
router = APIRouter(prefix="/api/gmail", tags=["gmail"])


@router.get("/status")
def gmail_status(db: Session = Depends(get_db)):
    try:
        oauth_configured = bool(oauth_config_svc.load(_DATA.config))
        oauth_state = "oauth_not_connected" if oauth_configured else "oauth_not_configured"
    except RuntimeError:
        oauth_configured = False
        oauth_state = "credential_key_unavailable"
    account, _oauth = resolve_sending_account(
        db, owner_id=ensure_owner(db), provision=False
    )
    if not account or not account.is_connected or not account.oauth:
        return {
            "connected": False,
            "email": None,
            "granted_scopes": None,
            "history_id": None,
            "is_demo": not is_gmail_configured(),
            "configured": oauth_configured,
            "oauth_state": oauth_state,
            "real_send": False,
            "initial_import_completed": False,
            "sync_state": "gmail_not_connected",
        }
    cipher = Cipher()
    access = cipher.decrypt(account.oauth.access_token_enc)
    connected = bool(access) and is_gmail_configured()
    latest_sync = db.query(models.GmailSyncRun).filter_by(
        gmail_account_id=account.id
    ).order_by(models.GmailSyncRun.id.desc()).first()
    initial_completed = sync_svc.initial_import_completed(db, account.id)
    return {
        "connected": connected,
        "email": account.email,
        "granted_scopes": (account.granted_scopes or "").split(",") if account.granted_scopes else [],
        "history_id": account.history_id,
        "is_demo": not is_gmail_configured(),
        "configured": oauth_configured,
        "oauth_state": "oauth_connected" if connected else oauth_state,
        "real_send": is_real_send_enabled(get_settings(), account, account.oauth) if connected else False,
        "initial_import_completed": initial_completed,
        "sync_state": (latest_sync.status if latest_sync else
                       ("incremental_ready" if initial_completed else "initial_import_required")),
    }


@router.get("/oauth/start")
def oauth_start(db: Session = Depends(get_db)):
    settings = get_settings()
    if not is_gmail_configured(settings):
        raise HTTPException(status_code=400, detail="Google Desktop OAuth is not configured. Import credentials.json in Agent Settings.")
    state = auth.generate_state()
    url = auth.build_authorization_url(state, settings)
    return {"url": url, "state": state}


class DesktopOAuthImport(BaseModel):
    credentials: dict


@router.get("/oauth-config")
def oauth_config():
    """Return non-sensitive customer Desktop OAuth configuration state."""
    try:
        item = oauth_config_svc.load(_DATA.config)
    except RuntimeError:
        return {
            "configured": False,
            "oauth_state": "credential_key_unavailable",
            "client_id_hint": "",
            "redirect_uri": oauth_config_svc.redirect_uri(get_settings().API_URL),
            "client_type": "installed",
        }
    client_id = (item or {}).get("client_id", "")
    configured = bool(item)
    return {
        "configured": configured,
        "oauth_state": "oauth_not_connected" if configured else "oauth_not_configured",
        "client_id_hint": client_id[-4:] if client_id else "",
        "redirect_uri": oauth_config_svc.redirect_uri(get_settings().API_URL),
        "client_type": "installed",
    }


@router.put("/oauth-config")
def oauth_config_update(body: DesktopOAuthImport):
    """Validate and DPAPI-protect a customer-owned Google Desktop OAuth JSON."""
    try:
        saved = oauth_config_svc.save(_DATA.config, body.credentials)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail="credential_key_unavailable") from exc
    get_settings.cache_clear()
    return {
        "ok": True,
        "configured": True,
        "oauth_state": "oauth_not_connected",
        "client_type": "installed",
        "client_id_hint": saved["client_id"][-4:],
        "redirect_uri": oauth_config_svc.redirect_uri(get_settings().API_URL),
    }

@router.get("/oauth/callback")
def oauth_callback(code: str = Query(...), state: str = Query(...), db: Session = Depends(get_db)):
    settings = get_settings()
    if not is_gmail_configured(settings):
        raise HTTPException(status_code=400, detail="Google OAuth not configured.")
    try:
        result = auth.exchange_code(code, state, settings)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Build a real transport to fetch the profile
    cipher = Cipher()
    # We need the real transport; build one from the credentials directly.
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(
        token=result.access_token, refresh_token=result.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID, client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=auth.SCOPES,
    )
    svc = build("gmail", "v1", credentials=creds)
    profile = svc.users().getProfile(userId="me").execute()
    email = profile["emailAddress"]

    owner_id = ensure_owner(db)
    account = db.query(models.GmailAccount).filter_by(user_id=owner_id, email=email).first()
    if account is None:
        account = models.GmailAccount(user_id=owner_id, email=email, is_connected=True,
                                       granted_scopes=",".join(result.scopes),
                                       history_id=profile.get("historyId"))
        db.add(account)
        db.flush()
    else:
        account.is_connected = True
        account.granted_scopes = ",".join(result.scopes)
        account.history_id = profile.get("historyId")

    # Upsert encrypted oauth credential
    oauth = account.oauth
    if oauth is None:
        oauth = models.OAuthCredential(gmail_account_id=account.id)
        db.add(oauth)
        db.flush()
    oauth.access_token_enc = cipher.encrypt(result.access_token)
    oauth.refresh_token_enc = cipher.encrypt(result.refresh_token) if result.refresh_token else oauth.refresh_token_enc
    from datetime import datetime, timezone
    oauth.token_expiry = datetime.fromtimestamp(result.token_expiry, tz=timezone.utc) if result.token_expiry else None
    # A successful customer-owned OAuth connection is the explicit operational
    # boundary for real delivery. Approval/pause/limit/suppression gates remain
    # unchanged and still run before every send.
    db.add(models.AuditLog(
        actor="user", action="real_send_enabled_gmail_connected",
        entity="gmail_account", entity_id=str(account.id),
        detail="enabled_by_verified_gmail_oauth_connection",
    ))
    db.commit()
    # Drop any transport cached before this connection. Without this, a
    # long-lived process keeps reusing the in-memory demo double that was
    # cached while the account was disconnected, and the app persists its fake
    # historyId (1000) as if Gmail had reported it.
    clear_transport_cache(account.id)
    logger.info("Gmail connected: %s (access %s)", email, redact_for_log(result.access_token))
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>Gmail connected</title>"
        "<style>body{font:16px system-ui;padding:48px;background:#080d16;color:#e8edf7}</style>"
        "<h1>Gmail authorization completed</h1><p>You can close this browser tab and return to Email Automation.</p>"
    )


def _sync_run_out(run: models.GmailSyncRun | None, db: Session, account_id: int | None = None):
    completed = False
    if account_id is not None:
        completed = sync_svc.initial_import_completed(db, account_id)
    if run is None:
        return {"run": None, "initial_import_completed": completed}
    return {
        "run": {
            "id": run.id, "kind": run.kind, "status": run.status,
            "phase": "completed" if run.status == "completed" else (
                "history_replay" if run.scan_completed else "scanning"
            ),
            "threads_scanned": run.threads_scanned,
            "new_threads": run.new_threads, "new_messages": run.new_messages,
            "failures": run.failures, "error": run.error,
            "can_pause": run.status in {"queued", "running"},
            "can_resume": run.status in {"paused", "failed"},
            "can_cancel": run.status in {"queued", "running", "paused"},
            "started_at": run.started_at, "finished_at": run.finished_at,
        },
        "initial_import_completed": completed,
    }


def _connected_account(db: Session):
    account, _oauth = resolve_sending_account(
        db, owner_id=ensure_owner(db), provision=False
    )
    if not account or not account.oauth:
        raise ApiError(409, "GMAIL_NOT_CONNECTED", "Connect Gmail before synchronizing mail.")
    return account


def _create_initial_import(db: Session, account):
    inflight = db.query(models.GmailSyncRun).filter(
        models.GmailSyncRun.gmail_account_id == account.id,
        models.GmailSyncRun.status.in_(("queued", "running", "paused")),
    ).order_by(models.GmailSyncRun.id.desc()).first()
    if inflight:
        return inflight, False
    run = models.GmailSyncRun(
        owner_id=account.user_id, gmail_account_id=account.id,
        kind="initial_full", status="queued",
        query=sync_svc.INITIAL_IMPORT_QUERY, include_spam_trash=False,
    )
    db.add(run)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        run = db.query(models.GmailSyncRun).filter(
            models.GmailSyncRun.gmail_account_id == account.id,
            models.GmailSyncRun.status.in_(("queued", "running", "paused")),
        ).order_by(models.GmailSyncRun.id.desc()).first()
        return run, False
    tasks.enqueue_gmail_initial_import(run.id)
    return run, True


@router.post("/imports")
def start_initial_import(db: Session = Depends(get_db)):
    account = _connected_account(db)
    run, created = _create_initial_import(db, account)
    return {"ok": True, "created": created, **_sync_run_out(run, db, account.id)}


@router.get("/imports/current")
def current_import(db: Session = Depends(get_db)):
    account, _oauth = resolve_sending_account(
        db, owner_id=ensure_owner(db), provision=False
    )
    if not account:
        return {"run": None, "initial_import_completed": False}
    run = db.query(models.GmailSyncRun).filter_by(
        gmail_account_id=account.id, kind="initial_full"
    ).order_by(models.GmailSyncRun.id.desc()).first()
    return _sync_run_out(run, db, account.id)


@router.post("/imports/{run_id}/pause")
def pause_import(run_id: int, db: Session = Depends(get_db)):
    account = _connected_account(db)
    run = db.get(models.GmailSyncRun, run_id)
    if not run or run.gmail_account_id != account.id or run.kind != "initial_full":
        raise HTTPException(status_code=404, detail="gmail_import_not_found")
    if run.status not in {"queued", "running"}:
        raise HTTPException(status_code=409, detail=f"gmail_import_is_{run.status}")
    run.status = "paused"
    db.commit()
    return {"ok": True, **_sync_run_out(run, db, account.id)}


@router.post("/imports/{run_id}/resume")
def resume_import(run_id: int, db: Session = Depends(get_db)):
    account = _connected_account(db)
    run = db.get(models.GmailSyncRun, run_id)
    if not run or run.gmail_account_id != account.id or run.kind != "initial_full":
        raise HTTPException(status_code=404, detail="gmail_import_not_found")
    if run.status not in {"paused", "failed"}:
        raise HTTPException(status_code=409, detail=f"gmail_import_is_{run.status}")
    run.status = "queued"
    run.error = None
    run.finished_at = None
    db.commit()
    tasks.enqueue_gmail_initial_import(run.id)
    return {"ok": True, **_sync_run_out(run, db, account.id)}


@router.post("/imports/{run_id}/cancel")
def cancel_import(run_id: int, db: Session = Depends(get_db)):
    account = _connected_account(db)
    run = db.get(models.GmailSyncRun, run_id)
    if not run or run.gmail_account_id != account.id or run.kind != "initial_full":
        raise HTTPException(status_code=404, detail="gmail_import_not_found")
    if run.status not in {"queued", "running", "paused"}:
        raise HTTPException(status_code=409, detail=f"gmail_import_is_{run.status}")
    run.status = "cancelled"
    run.finished_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, **_sync_run_out(run, db, account.id)}


@router.post("/sync")
def sync(db: Session = Depends(get_db), full_scan: bool = False):
    account = _connected_account(db)
    if full_scan:
        run, created = _create_initial_import(db, account)
        return {"ok": True, "created": created, "legacy_full_scan": True,
                **_sync_run_out(run, db, account.id)}
    if not sync_svc.initial_import_completed(db, account.id):
        raise ApiError(409, "INITIAL_IMPORT_REQUIRED",
                       "Complete the first full mailbox import before incremental sync.")
    inflight = db.query(models.GmailSyncRun).filter(
        models.GmailSyncRun.gmail_account_id == account.id,
        models.GmailSyncRun.status.in_(("queued", "running", "paused")),
    ).first()
    if inflight:
        raise ApiError(409, "GMAIL_SYNC_IN_PROGRESS", "Another Gmail synchronization is active.")
    cipher = Cipher()
    access = cipher.decrypt(account.oauth.access_token_enc)
    if not access:
        raise HTTPException(status_code=400, detail="Gmail tokens missing/undecryptable.")
    run = models.GmailSyncRun(
        owner_id=account.user_id, gmail_account_id=account.id,
        kind="incremental", status="running", started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    try:
        summary = sync_svc.sync_incremental(db, account, account.oauth)
    except Exception as e:
        db.rollback()
        run = db.get(models.GmailSyncRun, run.id)
        message = str(e)
        # A demo/stale cursor was persisted before OAuth completed. It cannot be
        # replayed (Gmail 404s it), so surface an explicit "re-import required"
        # instead of the generic failure path, and do not poison the run's
        # status with a fabricated cursor_expired that would loop forever.
        needs_import = "initial_import_required" in message.lower()
        cursor_expired = (not needs_import) and (
            "404" in message or ("historyid" in message.lower() and "invalid" in message.lower())
        )
        run.status = "cursor_expired" if cursor_expired else "failed"
        run.error = ("gmail_history_cursor_expired" if cursor_expired else message[:1000])
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        logger.warning("Gmail sync failed: %s", e)
        if needs_import:
            raise ApiError(409, "INITIAL_IMPORT_REQUIRED",
                           "The saved Gmail history cursor is invalid (stale pre-connection value). "
                           "Run a full mailbox import to re-establish a valid cursor.")
        code = "GMAIL_HISTORY_CURSOR_EXPIRED" if cursor_expired else "GMAIL_SYNC_FAILED"
        raise ApiError(409 if cursor_expired else 502, code,
                       "Gmail incremental cursor expired; a user-authorized history import is required."
                       if cursor_expired else f"Gmail sync failed: {message[:300]}")
    run.status = "completed"
    run.threads_scanned = summary["threads"]
    run.new_threads = summary["new_threads"]
    run.new_messages = summary["new_messages"]
    run.failures = summary["failures"]
    run.latest_history_id = summary["history_id"]
    run.finished_at = datetime.now(timezone.utc)
    db.commit()
    publish("sync", {"kind": "incremental", **summary})
    return {"ok": True, "kind": "incremental", "run_id": run.id, **summary}


@router.post("/disconnect")
def disconnect(db: Session = Depends(get_db)):
    account = (
        db.query(models.GmailAccount)
        .filter_by(is_connected=True)
        .order_by(models.GmailAccount.id.desc())
        .first()
    )
    if account:
        if account.oauth:
            db.delete(account.oauth)
        account.is_connected = False
        account.granted_scopes = None
        db.add(models.AuditLog(
            actor="user", action="real_send_disabled_gmail_disconnected",
            entity="gmail_account", entity_id=str(account.id),
            detail="disabled_until_a_verified_gmail_oauth_connection_exists",
        ))
        db.commit()
    return {"ok": True, "connected": False}
