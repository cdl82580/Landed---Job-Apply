"""
scripts/applications.py — Tigris S3 storage for job application tracking.

Key layout:
  applications/{user_id}/{app_id}.json   — full application record
  applications/{user_id}/_index.json     — summary list for fast listing
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from . import storage

# Per-user lock prevents concurrent index read-modify-write races.
# The dict itself is protected by a secondary meta-lock.
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

def _app_key(user_id: str, app_id: str) -> str:
    return f"applications/{user_id}/{app_id}.json"


def _index_key(user_id: str) -> str:
    return f"applications/{user_id}/_index.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_INDEX_FIELDS = {
    "id", "company", "domain", "company_logo_url", "role_title",
    "status", "date_applied", "last_updated", "created_at", "priority", "tags",
    "url", "recruiter_name", "recruiter_email", "location", "salary_range", "job_source",
    "linked_runs", "match_score", "status_changed_at",
}


MAX_TAGS = 20
MAX_TAG_LEN = 30


def normalize_tags(tags: list[str]) -> list[str]:
    """Trim, drop empties, dedupe case-insensitively (first casing wins).
    Raises ValueError if limits are exceeded."""
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        t = " ".join(str(t).split())
        if not t or t.lower() in seen:
            continue
        if len(t) > MAX_TAG_LEN:
            raise ValueError(f"tags must be {MAX_TAG_LEN} characters or fewer")
        seen.add(t.lower())
        out.append(t)
    if len(out) > MAX_TAGS:
        raise ValueError(f"at most {MAX_TAGS} tags per application")
    return out


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


def _remove_from_index(user_id: str, app_id: str) -> None:
    entries = _read_index(user_id)
    entries = [e for e in entries if e["id"] != app_id]
    _write_index(user_id, entries)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_applications(
    user_id: str,
    status: str | None = None,
    priority: str | None = None,
    page: int = 1,
    per_page: int = 0,   # 0 = all (no pagination)
) -> dict[str, Any]:
    from . import cache
    cache_key = f"app_index:{user_id}"
    cached = cache.get(cache_key) if not status and not priority and page == 1 and per_page == 0 else None
    if cached is not None:
        return cached
    entries = _read_index(user_id)
    if status:
        entries = [e for e in entries if e.get("status") == status]
    if priority:
        entries = [e for e in entries if e.get("priority") == priority]

    entries = sorted(entries, key=lambda e: e.get("last_updated", ""), reverse=True)
    total   = len(entries)

    if per_page and per_page > 0:
        pages  = max(1, (total + per_page - 1) // per_page)
        page   = max(1, min(page, pages))
        start  = (page - 1) * per_page
        items  = entries[start : start + per_page]
    else:
        pages = 1
        page  = 1
        items = entries

    result = {
        "items":    items,
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    pages,
    }
    if not status and not priority and page == 1 and per_page == 0:
        cache.put(cache_key, result)
    return result


def get_application(user_id: str, app_id: str) -> dict[str, Any] | None:
    from . import cache
    ck = f"app:{user_id}:{app_id}"
    cached = cache.get(ck)
    if cached is not None:
        return cached
    raw = storage.get_text(_app_key(user_id, app_id))
    if not raw:
        return None
    try:
        return cache.put(ck, json.loads(raw))
    except Exception:
        return None


def save_application(user_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Persist record and update the index. Returns a new dict with last_updated
    set — does not mutate the caller's object."""
    saved = {**record, "last_updated": _now()}
    with _user_lock(user_id):
        storage.put_text(_app_key(user_id, saved["id"]), json.dumps(saved))
        _upsert_index(user_id, saved)
    from . import cache
    cache.invalidate(f"app:{user_id}:{saved['id']}")
    cache.invalidate(f"app_index:{user_id}")
    return saved


def link_run(user_id: str, app_id: str, run_info: dict[str, Any]) -> dict[str, Any] | None:
    """Append a run link to an application. Returns updated record or None if not found."""
    with _user_lock(user_id):
        record = get_application(user_id, app_id)
        if not record:
            return None
        record.setdefault("linked_runs", []).append(run_info)
        record["last_updated"] = _now()
        storage.put_text(_app_key(user_id, record["id"]), json.dumps(record))
        _upsert_index(user_id, record)
    return record


def unlink_run(user_id: str, app_id: str, link_id: str) -> bool:
    """Remove a run link from an application. Returns True if removed."""
    with _user_lock(user_id):
        record = get_application(user_id, app_id)
        if not record:
            return False
        before = len(record.get("linked_runs", []))
        record["linked_runs"] = [r for r in record.get("linked_runs", []) if r["id"] != link_id]
        if len(record["linked_runs"]) == before:
            return False
        record["last_updated"] = _now()
        storage.put_text(_app_key(user_id, record["id"]), json.dumps(record))
        _upsert_index(user_id, record)
    return True


def save_match_score(user_id: str, app_id: str, match_score: dict[str, Any]) -> dict[str, Any] | None:
    """Store a resume<->JD match score on an application. Returns the updated
    record or None if not found. Does not bump last_updated — scoring is a
    derived analysis, not a user edit to the application itself."""
    with _user_lock(user_id):
        record = get_application(user_id, app_id)
        if not record:
            return None
        record["match_score"] = match_score
        storage.put_text(_app_key(user_id, record["id"]), json.dumps(record))
        _upsert_index(user_id, record)
    return record


def save_deleted_tombstone(user_id: str, record: dict[str, Any]) -> None:
    """Persist a deleted application under a separate key for audit purposes."""
    key = f"applications/{user_id}/_deleted/{record['id']}.json"
    storage.put_text(key, json.dumps(record))


def delete_application(user_id: str, app_id: str) -> bool:
    with _user_lock(user_id):
        if not storage.exists(_app_key(user_id, app_id)):
            return False
        try:
            storage.delete_bytes(_app_key(user_id, app_id))
        except Exception:
            return False
        _remove_from_index(user_id, app_id)
    from . import cache
    cache.invalidate(f"app:{user_id}:{app_id}")
    cache.invalidate(f"app_index:{user_id}")
    return True
