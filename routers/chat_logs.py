"""
routers/chat_logs.py — Admin-only visibility into KB support-chatbot conversations.

Written by chainlit_app.py after every message turn (see scripts/chat_logs.py).
Admin-only: these can contain a user's account details and whatever they typed,
so this is not exposed to regular users.

Endpoints:
  GET /api/admin/chat-logs                       — all conversations, all users
  GET /api/admin/chat-logs/{user_id}/{conv_id}    — full transcript
"""

from __future__ import annotations

import concurrent.futures

from fastapi import APIRouter, HTTPException, Request

from scripts import chat_logs as log_store
from scripts import storage

router = APIRouter(prefix="/api/admin/chat-logs", tags=["chat-logs"])


def _admin(request: Request) -> dict:
    from api import _require_admin  # noqa: PLC0415
    return _require_admin(request)


@router.get("")
async def list_all_conversations(request: Request):
    """All conversations across all users, newest first."""
    _admin(request)
    users = storage.list_all_users()

    def _fetch(u):
        uid = u["user_id"]
        try:
            return log_store.list_conversations(uid)
        except Exception:
            return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        batches = list(pool.map(_fetch, users))

    all_conversations = [c for batch in batches for c in batch]
    all_conversations.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
    return all_conversations


@router.get("/{user_id}/{conversation_id}")
async def get_conversation(user_id: str, conversation_id: str, request: Request):
    _admin(request)
    record = log_store.get_conversation(user_id, conversation_id)
    if not record:
        raise HTTPException(404, "Conversation not found")
    return record
