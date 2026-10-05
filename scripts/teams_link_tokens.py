"""
scripts/teams_link_tokens.py — Short-lived HMAC-signed tokens for linking a
Teams identity to an existing Landed account via the web login flow.

Used when a Teams user's own email has no matching Landed account: the bot
issues a token naming their aad_object_id (see routers/teams.py:
POST /api/teams/link-token), and https://apply.cdlav.us/teams-link.html lets
them sign in — password or Google — to claim it for whichever account they
log into (see api.py: POST /api/teams/link-claim). This is how a Teams
identity gets linked to an account under a different email than Teams reports.

Stateless (no storage round-trip) — signed with TEAMS_LINK_TOKEN_SECRET,
falling back to SESSION_SECRET. Short TTL since it's meant to be used
immediately after the bot sends it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

TOKEN_TTL_SECS = 15 * 60  # 15 minutes


def _secret() -> bytes:
    s = os.environ.get("TEAMS_LINK_TOKEN_SECRET") or os.environ.get("SESSION_SECRET", "")
    if not s:
        raise RuntimeError("Neither TEAMS_LINK_TOKEN_SECRET nor SESSION_SECRET is set")
    return s.encode()


def _sign(payload_b64: str) -> str:
    return hmac.new(_secret(), payload_b64.encode(), hashlib.sha256).hexdigest()


def create_token(aad_object_id: str, teams_email: str) -> str:
    """Return a signed URL-safe token string naming this Teams identity."""
    data = {
        "aad_object_id": aad_object_id,
        "teams_email":   teams_email,
        "expires_at":    int(time.time()) + TOKEN_TTL_SECS,
    }
    b64 = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    sig = _sign(b64)
    return f"{b64}.{sig}"


def verify_token(token: str) -> dict[str, Any] | None:
    """Verify signature and expiry. Returns the inner data dict or None."""
    try:
        b64, sig = token.rsplit(".", 1)
    except ValueError:
        return None

    expected = _sign(b64)
    if not hmac.compare_digest(expected, sig):
        return None

    try:
        data = json.loads(base64.urlsafe_b64decode(b64 + "=="))
    except Exception:
        return None

    if data.get("expires_at", 0) < time.time():
        return None

    return data
