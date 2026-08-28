"""Gmail OAuth + sync + disconnect endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..config import _DATA, get_settings, is_gmail_configured
from ..errors import ApiError
from ..events import publish
from ..gmail import auth
from ..security import Cipher, redact_for_log
from ..services import sync as sync_svc
from .. import oauth_config as oauth_config_svc
from ..services.accounts import resolve_sending_account
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
        }
    cipher = Cipher()
    access = cipher.decrypt(account.oauth.access_token_enc)
    connected = bool(access) and is_gmail_configured()
    return {
        "connected": connected,
        "email": account.email,
        "granted_scopes": (account.granted_scopes or "").split(",") if account.granted_scopes else [],
        "history_id": account.history_id,
        "is_demo": not is_gmail_configured(),
        "configured": oauth_configured,
        "oauth_state": "oauth_connected" if connected else oauth_state,
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
    db.commit()
    logger.info("Gmail connected: %s (access %s)", email, redact_for_log(result.access_token))
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>Gmail connected</title>"
        "<style>body{font:16px system-ui;padding:48px;background:#080d16;color:#e8edf7}</style>"
        "<h1>Gmail authorization completed</h1><p>You can close this browser tab and return to Email Automation.</p>"
    )


@router.post("/sync")
def sync(db: Session = Depends(get_db), query: str = "", max_results: int = 50, full_scan: bool = False):
    if full_scan:
        query = query or "in:anywhere"
        max_results = max(max_results, 500)
    account, _oauth = resolve_sending_account(
        db, owner_id=ensure_owner(db), provision=False
    )
    if not account or not account.oauth:
        # Offline mode: no connected Gmail account is available to sync.
        return {"ok": True, "offline": True, "threads": 0, "new_threads": 0, "new_messages": 0,
                "note": "No real Gmail connected. Connect OAuth to sync real mail."}
    cipher = Cipher()
    access = cipher.decrypt(account.oauth.access_token_enc)
    if not access:
        raise HTTPException(status_code=400, detail="Gmail tokens missing/undecryptable.")
    try:
        summary = sync_svc.sync_inbox(
            db, account, account.oauth, query=query, max_results=max_results,
            include_spam_trash=full_scan,
        )
    except Exception as e:
        logger.warning("Gmail sync failed: %s", e)
        raise ApiError(502, "GMAIL_SYNC_FAILED", f"Gmail sync failed: {str(e)[:300]}")
    db.commit()
    publish("sync", {"full_scan": full_scan, **summary})
    return {"ok": True, "full_scan": full_scan, **summary}


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
        db.commit()
    return {"ok": True, "connected": False}
