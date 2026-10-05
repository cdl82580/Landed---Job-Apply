"""
routers/teams.py — Microsoft Teams Bot Framework webhook.

Mounts the bot built in teams_bot/ directly onto the main FastAPI app so it
shares the web process's public domain/TLS cert instead of needing its own
Fly machine and port. teams_bot/ has no __init__.py and uses flat
`from config import Config` / `import api_client` imports (mirroring how it's
run standalone via `python app.py`), so we add it to sys.path once at import
time rather than rewriting it as a package.

  POST /api/messages             — Bot Framework webhook (Azure Bot -> here)
  POST /api/teams/link-status    — has this Teams identity been linked to a Landed account?
  POST /api/teams/account-lookup — does a Landed account exist for this email?
  POST /api/teams/link-confirm   — link a Teams identity to the account for this email
  POST /api/teams/link-token     — issue a short-lived token for the web login-linking flow
  POST /api/teams/unlink         — remove a Teams identity's link

The five /api/teams/* endpoints below are for the bot's own use (it calls
itself over HTTP — see teams_bot/api_client.py) and are gated on the shared
BOT_API_KEY directly, not on request.state.user: they resolve *other*
accounts by email, which a normal logged-in session has no business doing.

POST /api/teams/link-claim, the counterpart to link-token, is defined in
api.py instead (not here) since it authenticates via the caller's own
session cookie — it needs api.py's _require_user, and api.py already
imports this router, so importing back would be circular.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, TurnContext
from botbuilder.schema import Activity
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from scripts import storage, teams_link_tokens, teams_links
from scripts.session import verify_bot_key

_TEAMS_BOT_DIR = str(Path(__file__).resolve().parent.parent / "teams_bot")
if _TEAMS_BOT_DIR not in sys.path:
    sys.path.insert(0, _TEAMS_BOT_DIR)

from config import Config as TeamsConfig  # noqa: E402
from bot import JobApplyBot  # noqa: E402

router = APIRouter(tags=["teams"])


def _require_bot(request: Request) -> None:
    bot_key = os.environ.get("BOT_API_KEY", "")
    if not verify_bot_key(request.headers.get("Authorization", ""), bot_key):
        raise HTTPException(401, "Bot key required")


class _LinkStatusBody(BaseModel):
    aad_object_id: str


class _AccountLookupBody(BaseModel):
    email: str


class _LinkConfirmBody(BaseModel):
    aad_object_id: str
    email: str


class _UnlinkBody(BaseModel):
    aad_object_id: str


class _LinkTokenBody(BaseModel):
    aad_object_id: str
    teams_email: str


@router.post("/api/teams/link-status")
async def teams_link_status(body: _LinkStatusBody, request: Request):
    _require_bot(request)
    link = teams_links.get_link(body.aad_object_id)
    if not link:
        return {"linked": False}
    return {"linked": True, "email": link["email"], "expires_at": link["expires_at"]}


@router.post("/api/teams/account-lookup")
async def teams_account_lookup(body: _AccountLookupBody, request: Request):
    _require_bot(request)
    return {"exists": storage.get_user_by_email(body.email) is not None}


@router.post("/api/teams/link-confirm")
async def teams_link_confirm(body: _LinkConfirmBody, request: Request):
    _require_bot(request)
    user = storage.get_user_by_email(body.email)
    if not user:
        raise HTTPException(404, "No Landed account for that email")
    teams_links.save_link(body.aad_object_id, user["user_id"], user["email"])
    return {"linked": True, "email": user["email"]}


@router.post("/api/teams/link-token")
async def teams_link_token(body: _LinkTokenBody, request: Request):
    """Issue a short-lived token the bot can hand the user as a link into
    /teams-link.html, for when their Teams email has no matching account —
    they may still have an existing Landed account under a different
    email, so this lets them sign in (password or Google) to claim it."""
    _require_bot(request)
    token = teams_link_tokens.create_token(body.aad_object_id, body.teams_email)
    return {"token": token}


@router.post("/api/teams/unlink")
async def teams_unlink(body: _UnlinkBody, request: Request):
    _require_bot(request)
    teams_links.delete_link(body.aad_object_id)
    return {"ok": True}


_SETTINGS = BotFrameworkAdapterSettings(
    TeamsConfig.APP_ID,
    TeamsConfig.APP_PASSWORD,
    channel_auth_tenant=TeamsConfig.APP_TENANT_ID or None,
)
_ADAPTER = BotFrameworkAdapter(_SETTINGS)
_BOT = JobApplyBot()


async def _on_error(context: TurnContext, error: Exception):
    print(f"\n[teams on_turn_error] unhandled error: {error}", file=sys.stderr)
    traceback.print_exc()
    await context.send_activity("Sorry, something went wrong. Please try again.")


_ADAPTER.on_turn_error = _on_error


@router.post("/api/messages")
async def teams_messages(request: Request):
    if "application/json" not in request.headers.get("Content-Type", ""):
        raise HTTPException(status_code=415, detail="Unsupported Media Type")

    body = await request.json()
    activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization", "")

    try:
        invoke_response = await _ADAPTER.process_activity(activity, auth_header, _BOT.on_turn)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if invoke_response:
        return Response(
            content=json.dumps(invoke_response.body) if invoke_response.body else None,
            status_code=invoke_response.status,
            media_type="application/json",
        )
    return Response(status_code=201)
