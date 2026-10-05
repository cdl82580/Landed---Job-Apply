"""HTTP client for the Landed FastAPI backend — mirrors slack_bot.py helpers.

Every call carries the shared BOT_API_KEY. Calls made on behalf of a linked
Teams user also carry X-Teams-User-Email so the API resolves that specific
account (see api.py:_bot_user) instead of the single primary account.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from config import Config


def _api(method: str, path: str, user_email: str | None = None, **kwargs) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {Config.BOT_API_KEY}"
    if user_email:
        headers["X-Teams-User-Email"] = user_email
    return getattr(requests, method)(
        f"{Config.API_BASE}{path}", headers=headers, timeout=30, **kwargs,
    )


# ── Teams identity linking ──────────────────────────────────────────────
# No user_email — these establish/inspect the link itself.

def teams_link_status(aad_object_id: str) -> dict:
    r = _api("post", "/api/teams/link-status", json={"aad_object_id": aad_object_id})
    r.raise_for_status()
    return r.json()


def teams_account_lookup(email: str) -> dict:
    r = _api("post", "/api/teams/account-lookup", json={"email": email})
    r.raise_for_status()
    return r.json()


def teams_link_confirm(aad_object_id: str, email: str) -> dict:
    r = _api("post", "/api/teams/link-confirm", json={"aad_object_id": aad_object_id, "email": email})
    if r.status_code == 404:
        return {"linked": False}
    r.raise_for_status()
    return r.json()


def teams_unlink(aad_object_id: str) -> None:
    r = _api("post", "/api/teams/unlink", json={"aad_object_id": aad_object_id})
    r.raise_for_status()


def teams_link_token(aad_object_id: str, teams_email: str) -> str:
    """Get a short-lived token for the web login-linking flow (teams-link.html)."""
    r = _api("post", "/api/teams/link-token", json={
        "aad_object_id": aad_object_id, "teams_email": teams_email,
    })
    r.raise_for_status()
    return r.json()["token"]


# ── Agent runs ───────────────────────────────────────────────────────────

def post_run(job_posting: str, company: str, role: str, contact: str = "",
             domain: str = "", user_email: str | None = None) -> dict:
    r = _api("post", "/api/run", user_email=user_email, json={
        "job_posting": job_posting,
        "company": company,
        "role": role,
        "contact": contact or None,
        "domain": domain or None,
    })
    r.raise_for_status()
    return r.json()


def poll_run(run_id: str, timeout: int = 300, user_email: str | None = None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api("get", f"/api/run/{run_id}/status", user_email=user_email)
        r.raise_for_status()
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(5)
    return {"status": "timeout", "error": "Timed out waiting for run to complete"}


def post_prep(job_posting: str, company: str, role: str,
              round_type: str, focus: str = "", interviewer: str = "",
              interview_date: str = "", interview_time: str = "", location: str = "",
              domain: str = "", user_email: str | None = None) -> dict:
    r = _api("post", "/api/prep", user_email=user_email, json={
        "job_posting": job_posting,
        "company": company,
        "role": role,
        "round_type": round_type,
        "focus": focus or None,
        "interviewer": interviewer or None,
        "interview_date": interview_date or None,
        "interview_time": interview_time or None,
        "location": location or None,
        "domain": domain or None,
    })
    r.raise_for_status()
    return r.json()


def poll_prep(prep_id: str, timeout: int = 300, user_email: str | None = None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api("get", f"/api/prep/{prep_id}/status", user_email=user_email)
        r.raise_for_status()
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(5)
    return {"status": "timeout", "error": "Timed out waiting for prep to complete"}


def post_aq(question: str, job_posting: str, company: str, role: str,
            tone: str = "professional", char_limit: int | None = None,
            user_email: str | None = None) -> dict:
    payload: dict[str, Any] = {
        "question": question,
        "job_posting": job_posting,
        "company": company,
        "role": role,
        "tone": tone,
    }
    if char_limit:
        payload["char_limit"] = char_limit
    r = _api("post", "/api/aq", user_email=user_email, json=payload)
    r.raise_for_status()
    return r.json()


def poll_aq(aq_id: str, timeout: int = 300, user_email: str | None = None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api("get", f"/api/aq/{aq_id}/status", user_email=user_email)
        r.raise_for_status()
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(5)
    return {"status": "timeout", "error": "Timed out waiting for answer to complete"}


# ── Company search (track add typeahead) ─────────────────────────────────

def search_companies(query: str) -> list[dict]:
    """[{name, domain, description, logo_url}] — public endpoint, no auth needed."""
    r = _api("get", "/api/companies/search", params={"q": query})
    r.raise_for_status()
    return r.json()


# ── Tracker ──────────────────────────────────────────────────────────────

def get_applications(status: str | None = None, user_email: str | None = None) -> list[dict]:
    params = {}
    if status:
        params["status"] = status
    r = _api("get", "/api/applications", user_email=user_email, params=params)
    r.raise_for_status()
    return r.json().get("items", [])


def get_application(app_id: str, user_email: str | None = None) -> dict:
    r = _api("get", f"/api/applications/{app_id}", user_email=user_email)
    r.raise_for_status()
    return r.json()


def create_application(data: dict, user_email: str | None = None) -> dict:
    r = _api("post", "/api/applications", user_email=user_email, json=data)
    r.raise_for_status()
    return r.json()


def update_application(app_id: str, updates: dict, user_email: str | None = None) -> dict:
    r = _api("put", f"/api/applications/{app_id}", user_email=user_email, json=updates)
    r.raise_for_status()
    return r.json()


def delete_application(app_id: str, user_email: str | None = None) -> None:
    r = _api("delete", f"/api/applications/{app_id}", user_email=user_email)
    r.raise_for_status()


def add_comment(app_id: str, text: str, user_email: str | None = None) -> dict:
    r = _api("post", f"/api/applications/{app_id}/comments", user_email=user_email, json={"text": text})
    r.raise_for_status()
    return r.json()


def score_application(app_id: str, user_email: str | None = None) -> dict:
    """Synchronous — resolves the JD itself server-side (linked Drive folder,
    or the application's posting URL). Returns {dimensions, score, category,
    rationale, scored_at, scored_by}."""
    r = _api("post", f"/api/applications/{app_id}/score", user_email=user_email)
    r.raise_for_status()
    return r.json()


# ── Calendar ─────────────────────────────────────────────────────────────

def get_calendar_events(from_dt: str | None = None, to_dt: str | None = None,
                         user_email: str | None = None) -> list[dict]:
    # GET /api/calendar's query params are aliased to "from"/"to" (routers/calendar.py)
    # — matches its docstring contract and what frontend/calendar.html already sends.
    params: dict[str, str] = {}
    if from_dt:
        params["from"] = from_dt
    if to_dt:
        params["to"] = to_dt
    r = _api("get", "/api/calendar", user_email=user_email, params=params)
    r.raise_for_status()
    return r.json().get("events", [])


def get_upcoming_events(user_email: str | None = None) -> list[dict]:
    r = _api("get", "/api/calendar/upcoming", user_email=user_email)
    r.raise_for_status()
    return r.json().get("events", [])


def create_calendar_event(payload: dict, user_email: str | None = None) -> dict:
    r = _api("post", "/api/calendar", user_email=user_email, json=payload)
    r.raise_for_status()
    return r.json()


def get_calendar_event(event_id: str, user_email: str | None = None) -> dict:
    r = _api("get", f"/api/calendar/{event_id}", user_email=user_email)
    r.raise_for_status()
    return r.json()


def delete_calendar_event(event_id: str, user_email: str | None = None) -> None:
    r = _api("delete", f"/api/calendar/{event_id}", user_email=user_email)
    r.raise_for_status()


# ── Thank-you email ──────────────────────────────────────────────────────

def post_thankyou(job_posting: str, company: str, role: str, round_type: str,
                   interviewer: str = "", topics: str = "", tone: str = "professional",
                   app_id: str | None = None, user_email: str | None = None) -> dict:
    payload: dict[str, Any] = {
        "job_posting": job_posting,
        "company": company,
        "role": role,
        "round_type": round_type,
        "interviewer": interviewer or None,
        "topics": topics or None,
        "tone": tone,
    }
    if app_id:
        payload["app_id"] = app_id
    r = _api("post", "/api/thankyou", user_email=user_email, json=payload)
    r.raise_for_status()
    return r.json()


def poll_thankyou(ty_id: str, timeout: int = 300, user_email: str | None = None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api("get", f"/api/thankyou/{ty_id}/status", user_email=user_email)
        r.raise_for_status()
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(5)
    return {"status": "timeout", "error": "Timed out waiting for thank-you email to complete"}


# ── Profile ──────────────────────────────────────────────────────────────

def get_profile(user_email: str | None = None) -> dict:
    r = _api("get", "/api/profile", user_email=user_email)
    r.raise_for_status()
    return r.json()


def update_profile(updates: dict, user_email: str | None = None) -> dict:
    r = _api("put", "/api/profile", user_email=user_email, json=updates)
    r.raise_for_status()
    return r.json()


def upload_resume(filename: str, file_bytes: bytes, user_email: str | None = None) -> dict:
    r = _api(
        "post", "/api/profile/resume", user_email=user_email,
        files={"resume": (
            filename, file_bytes,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )},
    )
    r.raise_for_status()
    return r.json()


# ── Optimize ─────────────────────────────────────────────────────────────

def post_optimize(app_id: str, folder_id: str, instruction: str,
                  company: str, role: str,
                  optimize_resume: bool = True,
                  optimize_cover_letter: bool = True,
                  user_email: str | None = None) -> dict:
    r = _api("post", "/api/optimize", user_email=user_email, json={
        "app_id": app_id,
        "folder_id": folder_id,
        "instruction": instruction,
        "company": company,
        "role": role,
        "optimize_resume": optimize_resume,
        "optimize_cover_letter": optimize_cover_letter,
    })
    r.raise_for_status()
    return r.json()


def poll_optimize(optimize_id: str, timeout: int = 300, user_email: str | None = None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api("get", f"/api/optimize/{optimize_id}/status", user_email=user_email)
        r.raise_for_status()
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(5)
    return {"status": "timeout", "error": "Timed out waiting for optimize to complete"}


# ── Agent runs ───────────────────────────────────────────────────────────

def get_agent_runs(user_email: str | None = None) -> list[dict]:
    r = _api("get", "/api/agent-runs", user_email=user_email)
    r.raise_for_status()
    return r.json().get("runs", [])


# ── Runs (legacy) ────────────────────────────────────────────────────────

def get_drive_runs(user_email: str | None = None) -> list[dict]:
    r = _api("get", "/api/gdrive/runs", user_email=user_email)
    r.raise_for_status()
    return r.json()


def get_job_posting(folder_id: str, user_email: str | None = None) -> str | None:
    """Saved job posting text for a Drive folder, or None if none is saved yet."""
    r = _api("get", f"/api/gdrive/runs/{folder_id}/job_posting", user_email=user_email)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json().get("job_posting")
