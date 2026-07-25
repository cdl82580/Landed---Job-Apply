"""
scripts/agent_runs.py — Persistent agent run records in Tigris S3.

Key layout:
  agent-runs/users/{user_id}/{run_id}.json

Each run is a single JSON object written at creation and overwritten on
status transitions (queued → running → completed/failed).  This replaces
the audit-event-stitching approach for the admin "All Agent Runs" view.
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import storage

# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

def _run_key(user_id: str, run_id: str) -> str:
    return f"agent-runs/users/{user_id}/{run_id}.json"


def _user_prefix(user_id: str) -> str:
    return f"agent-runs/users/{user_id}/"


_ALL_PREFIX = "agent-runs/users/"

# ---------------------------------------------------------------------------
# Valid values
# ---------------------------------------------------------------------------

VALID_TYPES = frozenset({
    "resume", "interview_prep", "aq", "optimize", "thank_you", "scoring",
    "resume_builder",
})

VALID_STATUSES = frozenset({
    "queued", "running", "completed", "failed",
})

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def create(
    *,
    run_id: str,
    run_type: str,
    user_id: str,
    user_email: str,
    company: str = "",
    role: str = "",
    app_id: str = "",
    initiated_by: str = "",
    gdrive_folder_id: str = "",
    gdrive_folder_url: str = "",
    round_type: str = "",
    optimize_instruction: str = "",
    score: float | None = None,
    score_category: str = "",
    score_reasoning: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create and persist a new agent run record. Returns the record dict."""
    record: dict[str, Any] = {
        "id":                   run_id,
        "type":                 run_type,
        "user_id":              user_id,
        "user_email":           user_email,
        "company":              company,
        "role":                 role,
        "app_id":               app_id,
        "initiated_by":         initiated_by,
        "started_at":           _now_iso(),
        "finished_at":          None,
        "status":               "queued",
        "error":                None,
        "gdrive_folder_id":     gdrive_folder_id,
        "gdrive_folder_url":    gdrive_folder_url,
        "output_files":         [],
        "score":                score,
        "score_category":       score_category,
        "score_reasoning":      score_reasoning,
        "round_type":           round_type,
        "optimize_instruction": optimize_instruction,
    }
    if extra:
        record.update(extra)
    _save(user_id, run_id, record)
    return record


def update(user_id: str, run_id: str, **fields: Any) -> dict[str, Any] | None:
    """Update specific fields on an existing run record. Returns updated record or None."""
    record = get(user_id, run_id)
    if record is None:
        return None
    record.update(fields)
    _save(user_id, run_id, record)
    return record


def complete(
    user_id: str,
    run_id: str,
    *,
    gdrive_folder_id: str = "",
    gdrive_folder_url: str = "",
    output_files: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any] | None:
    """Mark a run as completed with optional output metadata."""
    fields: dict[str, Any] = {
        "status": "completed",
        "finished_at": _now_iso(),
    }
    if gdrive_folder_id:
        fields["gdrive_folder_id"] = gdrive_folder_id
    if gdrive_folder_url:
        fields["gdrive_folder_url"] = gdrive_folder_url
    if output_files is not None:
        fields["output_files"] = output_files
    fields.update(extra)
    return update(user_id, run_id, **fields)


def fail(user_id: str, run_id: str, error: str) -> dict[str, Any] | None:
    """Mark a run as failed."""
    return update(user_id, run_id, status="failed", finished_at=_now_iso(), error=error)


def get(user_id: str, run_id: str) -> dict[str, Any] | None:
    """Fetch a single run record, or None if not found."""
    raw = storage.get_text(_run_key(user_id, run_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def list_for_user(user_id: str) -> list[dict[str, Any]]:
    """Return all run records for a user, newest first."""
    runs: list[dict[str, Any]] = []
    for key in storage.list_keys(_user_prefix(user_id)):
        raw = storage.get_text(key)
        if not raw:
            continue
        try:
            runs.append(json.loads(raw))
        except Exception:
            pass
    runs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return runs


def list_all() -> list[dict[str, Any]]:
    """Return all run records across all users, newest first."""
    from . import cache
    cached = cache.get("agent_runs_all")
    if cached is not None:
        return cached
    runs: list[dict[str, Any]] = []
    for key in storage.list_keys(_ALL_PREFIX):
        raw = storage.get_text(key)
        if not raw:
            continue
        try:
            runs.append(json.loads(raw))
        except Exception:
            pass
    runs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return cache.put("agent_runs_all", runs)


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _save(user_id: str, run_id: str, record: dict[str, Any]) -> None:
    if not storage.is_configured():
        return
    try:
        storage.put_text(_run_key(user_id, run_id), json.dumps(record))
        from . import cache
        cache.invalidate("agent_runs_all")
    except Exception:
        pass
