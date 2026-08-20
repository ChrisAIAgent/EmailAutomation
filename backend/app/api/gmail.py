"""Gmail OAuth + sync + disconnect endpoints."""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..config import _BACKEND_DIR, get_settings, is_gmail_configured
from ..errors import ApiError
from ..events import publish
from ..gmail import auth
from ..security import Cipher, redact_for_log
from ..services import sync as sync_svc
from ..services.accounts import resolve_sending_account
from .deps import get_db, ensure_owner

logger = logging.getLogger("api.gmail")
router = APIRouter(prefix="/api/gmail", tags=["gmail"])

# .env write lock: the Gmail OAuth config PUT below mutates backend/.env.
_env_write_lock = threading.Lock()


@router.get("/status")
def gmail_status(db: Session = Depends(get_db)):
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
            "configured": is_gmail_configured(),
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
        "configured": is_gmail_configured(),
    }


@router.get("/oauth/start")
def oauth_start(db: Session = Depends(get_db)):
    settings = get_settings()
    if not is_gmail_configured(settings):
        raise HTTPException(status_code=400, detail="Google OAuth not configured (set GOOGLE_CLIENT_ID/SECRET in Settings).")
    state = auth.generate_state()
    url = auth.build_authorization_url(state, settings)
    return {"url": url, "state": state}


class GmailOAuthConfig(BaseModel):
    client_id: str
    client_secret: str
    redirect_uri: str | None = None


@router.get("/oauth-config")
def oauth_config():
    """Return Gmail OAuth configuration status WITHOUT secrets.

    Used by the Web UI to show whether Google OAuth is configured and which
    redirect URI must be registered in Google Cloud Console. Never returns the
    client secret.
    """
    settings = get_settings()
    configured = is_gmail_configured(settings)
    cid = settings.GOOGLE_CLIENT_ID or ""
    return {
        "configured": configured,
        "client_id_hint": (cid[-4:] if len(cid) >= 4 else "") if cid else "",
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
    }


@router.put("/oauth-config")
def oauth_config_update(body: GmailOAuthConfig):
    """Persist Google OAuth credentials to backend/.env and hot-reload config.

    Runs only on the target machine at runtime (the packaged installer never
    ships a .env). After writing, the cached Settings is cleared so the connect
    flow picks up the new credentials immediately.
    """
    client_id = (body.client_id or "").strip()
    client_secret = (body.client_secret or "").strip()
    redirect_uri = (body.redirect_uri or "").strip() or "http://127.0.0.1:8000/api/gmail/oauth/callback"
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="client_id and client_secret are required.")
    if len(client_id) < 10 or len(client_secret) < 10:
        raise HTTPException(status_code=400, detail="client_id/client_secret look malformed.")

    env_path = os.path.join(_BACKEND_DIR, ".env")
    with _env_write_lock:
        existing: dict[str, str] = {}
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    existing[k.strip()] = v.strip().strip('"').strip("'")
        existing["GOOGLE_CLIENT_ID"] = client_id
        existing["GOOGLE_CLIENT_SECRET"] = client_secret
        existing["GOOGLE_REDIRECT_URI"] = redirect_uri
        # Preserve ordering with the three OAuth keys last.
        ordered_keys = [k for k in existing if k not in (
            "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI")]
        ordered_keys += ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI"]
        tmp_path = env_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            for k in ordered_keys:
                fh.write(f"{k}={existing[k]}\n")
        os.replace(tmp_path, env_path)
        # Lock the file so only the current Windows user can read it.
        try:
            subprocess.run(
                ["icacls", env_path, "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME', '*')}:R"],
                check=False, capture_output=True,
            )
        except Exception:  # noqa: BLE001 - best-effort hardening
            pass
    # Hot-reload: drop the cached Settings so subsequent reads see the new .env.
    get_settings.cache_clear()
    return {"ok": True, "configured": True, "redirect_uri": redirect_uri}


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
    from ..gmail.transport import InMemoryGmailTransport
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
    account = db.query(models.GmailAccount).filter_by(email=email).first()
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
    return RedirectResponse(f"{settings.APP_URL}/?gmail=connected")


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
