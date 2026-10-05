"""
scripts/teams_links.py — Maps a Teams (Azure AD) identity to a Landed account.

Key layout:
  teams_links/{aad_object_id}.json   — {user_id, email, confirmed_at, expires_at}

A link is created only after the Teams user explicitly replies "confirm" to a
prompt naming the Landed account we found for their email (see
routers/teams.py and teams_bot/bot.py). Links expire after LINK_DAYS and must
be re-confirmed.
"""
from __future__ import annotations

import json
import time
from typing import Any

from . import storage

LINK_DAYS = 30


def get_link(aad_object_id: str) -> dict[str, Any] | None:
    """Return the linked account record, or None if missing or expired."""
    data = storage.get_text(f"teams_links/{aad_object_id}.json")
    if not data:
        return None
    link = json.loads(data)
    if link.get("expires_at", 0) < time.time():
        return None
    return link


def save_link(aad_object_id: str, user_id: str, email: str) -> dict[str, Any]:
    now = time.time()
    link = {
        "user_id":      user_id,
        "email":        email,
        "confirmed_at": now,
        "expires_at":   now + 86400 * LINK_DAYS,
    }
    storage.put_text(f"teams_links/{aad_object_id}.json", json.dumps(link))
    return link


def delete_link(aad_object_id: str) -> None:
    storage.delete_text(f"teams_links/{aad_object_id}.json")
