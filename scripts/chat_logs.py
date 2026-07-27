"""
scripts/chat_logs.py — Tigris S3 storage for KB support-chatbot conversations.

Key layout:
  chat-logs/{user_id}/{conversation_id}.json   — full transcript + escalations
  chat-logs/{user_id}/_index.json              — summary list for fast listing

Mirrors scripts/applications.py's shape. Written by chainlit_app.py (the KB
chatbot) after every message turn; read by routers/chat_logs.py for the admin
portal's Chat Logs tab.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from . import storage

_user_locks: dict[str, threading.Lock] = {}
_user_locks_mu = threading.Lock()


def _user_lock(user_id: str) -> threading.Lock:
    with _user_locks_mu:
        if user_id not in _user_locks:
            _user_locks[user_id] = threading.Lock()
        return _user_locks[user_id]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conv_key(user_id: str, conversation_id: str) -> str:
    return f"chat-logs/{user_id}/{conversation_id}.json"


def _index_key(user_id: str) -> str:
    return f"chat-logs/{user_id}/_index.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_INDEX_FIELDS = {
    "id", "user_id", "user_email", "user_role", "started_at", "updated_at",
    "message_count", "escalation_count", "escalation_reasons", "last_message_preview",
}


def _to_index_entry(record: dict[str, Any]) -> dict[str, Any]:
    return {k: record[k] for k in _INDEX_FIELDS if k in record}


# ---------------------------------------------------------------------------
# Index operations
# ---------------------------------------------------------------------------

def _read_index(user_id: str) -> list[dict[str, Any]]:
    raw = storage.get_text(_index_key(user_id))
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _write_index(user_id: str, entries: list[dict[str, Any]]) -> None:
    storage.put_text(_index_key(user_id), json.dumps(entries))


def _upsert_index(user_id: str, record: dict[str, Any]) -> None:
    entries = _read_index(user_id)
    entry = _to_index_entry(record)
    for i, e in enumerate(entries):
        if e["id"] == record["id"]:
            entries[i] = entry
            break
    else:
        entries.append(entry)
    _write_index(user_id, entries)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_conversations(user_id: str) -> list[dict[str, Any]]:
    entries = _read_index(user_id)
    return sorted(entries, key=lambda e: e.get("updated_at", ""), reverse=True)


def get_conversation(user_id: str, conversation_id: str) -> dict[str, Any] | None:
    raw = storage.get_text(_conv_key(user_id, conversation_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def save_conversation(record: dict[str, Any]) -> dict[str, Any]:
    """Upsert a conversation's full record and its index entry. `record` must
    include `id` and `user_id`. Sets/refreshes `updated_at`, `message_count`,
    `escalation_count`, `escalation_reasons`, and `last_message_preview` from
    `messages`/`escalations` before writing. Returns the saved record."""
    user_id = record["user_id"]
    messages = record.get("messages", [])
    escalations = record.get("escalations", [])

    saved = {
        **record,
        "updated_at": _now(),
        "message_count": len(messages),
        "escalation_count": len(escalations),
        "escalation_reasons": sorted({e["reason"] for e in escalations if e.get("reason")}),
        "last_message_preview": (messages[-1]["text"][:160] if messages else ""),
    }
    with _user_lock(user_id):
        storage.put_text(_conv_key(user_id, saved["id"]), json.dumps(saved))
        _upsert_index(user_id, saved)
    return saved
