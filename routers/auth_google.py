"""
routers/auth_google.py — Google OAuth 2.0 login.

Flow:
  GET /api/auth/google           → redirect to Google consent screen
  GET /api/auth/google/callback  → exchange code, find/create user, set session cookie

Env vars required:
  GOOGLE_CLIENT_ID
  GOOGLE_CLIENT_SECRET
  APP_URL  (e.g. https://apply.cdlav.us)
"""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.parse
import uuid

import requests
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

from scripts import storage
from scripts import user_audit
from scripts.session import SESSION_DAYS as _SESSION_DAYS, create_session_token
from scripts.session import resolve_session_secret

router = APIRouter(tags=["auth"])

_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
_APP_URL       = os.environ.get("APP_URL", "https://apply.cdlav.us")
_REDIRECT_URI  = f"{_APP_URL}/api/auth/google/callback"

_GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

_SESSION_COOKIE = "session"


_NONCE_COOKIE   = "oauth_nonce"
_NONCE_MAX_AGE  = 600  # 10 minutes


@router.get("/api/auth/google")
async def google_login(request: Request, returnTo: str = "/"):
    if not _CLIENT_ID or not _CLIENT_SECRET:
        return RedirectResponse(
            f"{_APP_URL}/login.html?auth_error=Google+OAuth+not+configured",
            status_code=302,
        )

    if not returnTo.startswith("/") or returnTo.startswith("//"):
        returnTo = "/"

    nonce  = secrets.token_urlsafe(16)
    state  = json.dumps({"returnTo": returnTo, "nonce": nonce})
    params = {
        "client_id":     _CLIENT_ID,
        "redirect_uri":  _REDIRECT_URI,
        "response_type": "code",
        "scope":         "openid email profile",
        "access_type":   "offline",
        "prompt":        "select_account",
        "state":         state,
    }
    url = f"{_GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        _NONCE_COOKIE, nonce,
        max_age=_NONCE_MAX_AGE,
        httponly=True, samesite="lax", secure=True,
    )
    return response


@router.get("/api/auth/google/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    error: str | None = None,
    state: str | None = None,
):
    fail_base = f"{_APP_URL}/login.html?auth_error="

    return_to = "/"
    state_nonce: str | None = None
    if state:
        if len(state) > 512:
            return RedirectResponse(
                f"{fail_base}{urllib.parse.quote('Invalid login session.')}",
                status_code=302,
            )
        try:
            parsed    = json.loads(state)
            return_to = parsed.get("returnTo", "/")
            state_nonce = parsed.get("nonce")
        except Exception:
            pass
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/"

    if error or not code:
        msg = urllib.parse.quote(error or "cancelled")
        return RedirectResponse(f"{fail_base}{msg}", status_code=302)

    # Verify CSRF nonce — cookie must match state param
    cookie_nonce = request.cookies.get(_NONCE_COOKIE)
    if not state_nonce or not cookie_nonce or not secrets.compare_digest(state_nonce, cookie_nonce):
        return RedirectResponse(
            f"{fail_base}{urllib.parse.quote('Invalid or expired login session. Please try again.')}",
            status_code=302,
        )

    # Exchange code for tokens
    try:
        token_resp = requests.post(
            _GOOGLE_TOKEN_URL,
            data={
                "code":          code,
                "client_id":     _CLIENT_ID,
                "client_secret": _CLIENT_SECRET,
                "redirect_uri":  _REDIRECT_URI,
                "grant_type":    "authorization_code",
            },
            timeout=10,
        )
        token_resp.raise_for_status()
        tokens = token_resp.json()
    except Exception as exc:
        msg = urllib.parse.quote(f"Token exchange failed: {exc}")
        return RedirectResponse(f"{fail_base}{msg}", status_code=302)

    # Verify the id_token locally using google-auth (caches Google's public keys)
    try:
        info = google_id_token.verify_oauth2_token(
            tokens["id_token"],
            google_requests.Request(),
            _CLIENT_ID,
        )
    except Exception as exc:
        msg = urllib.parse.quote(f"Token verification failed: {exc}")
        return RedirectResponse(f"{fail_base}{msg}", status_code=302)

    google_id = info.get("sub", "")
    email     = info.get("email", "").strip().lower()
    name      = info.get("name", email.split("@")[0])

    if not email or not info.get("email_verified"):
        return RedirectResponse(
            f"{fail_base}{urllib.parse.quote('Google account email is not verified')}",
            status_code=302,
        )

    # Find or create user
    xff   = request.headers.get("X-Forwarded-For", "")
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    ip    = parts[-1] if parts else (request.client.host if request.client else None)
    user = storage.get_user_by_google_id(google_id)

    if not user:
        # Check for existing account with same email — link it
        user = storage.get_user_by_email(email)
        if user:
            user["google_id"]      = google_id
            user["email_verified"] = True   # Google confirmed this email
            storage.save_user(user)
            user_audit.log(user["user_id"], "google_account_linked", email, ip,
                           google_id=google_id)
        else:
            # Brand-new user via Google — Google has already verified the email
            user_id = str(uuid.uuid4())
            user = {
                "user_id":        user_id,
                "email":          email,
                "display_name":   name,
                "password_hash":  f"google:{secrets.token_hex(32)}",  # unusable placeholder
                "google_id":      google_id,
                "created_at":     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "email_verified": True,
            }
            storage.save_user(user)
            user_audit.log(user_id, "user_registered_google", email, ip,
                           display_name=name, google_id=google_id)
    else:
        user_audit.log(user["user_id"], "login_google", email, ip)

    # Shared, memoized secret resolution (scripts/session.py) — avoids api.py
    # and this module each generating their own independent random fallback
    # when SESSION_SECRET is unset, which would make tokens cross-unverifiable.
    _session_secret  = resolve_session_secret()
    _fly_machine_id  = os.environ.get("FLY_MACHINE_ID", "")

    role = user.get("role", "user")
    # Admins are always sent to the admin dashboard regardless of returnTo
    if role == "admin":
        return_to = "/admin.html"
    token = create_session_token(user["user_id"], email, _session_secret, role=role,
                                 password_hash=user.get("password_hash", ""))
    response = RedirectResponse(f"{_APP_URL}{return_to}", status_code=302)
    response.set_cookie(
        _SESSION_COOKIE, token,
        max_age=86400 * _SESSION_DAYS,
        httponly=True, samesite="lax", secure=True,
    )
    response.delete_cookie(_NONCE_COOKIE)   # consumed — clear it
    if _fly_machine_id:
        response.set_cookie("fly-force-instance-id", _fly_machine_id, path="/", samesite="lax", httponly=True, secure=True)
    return response
