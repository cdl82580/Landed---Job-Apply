"""
routers/admin.py — Admin-only API endpoints.

All endpoints require role == "admin" in the session token.

Endpoints:
  GET  /api/admin/users                                  — list all users
  PUT  /api/admin/users/{user_id}/role                   — set user role
  GET  /api/admin/applications                           — all apps, all users
  GET  /api/admin/users/{user_id}/applications           — one user's apps
  GET  /api/admin/applications/{user_id}/{app_id}        — full record
  PUT  /api/admin/applications/{user_id}/{app_id}        — update
  DELETE /api/admin/applications/{user_id}/{app_id}      — delete
  GET  /api/admin/runs                                   — active in-memory runs
"""

from __future__ import annotations

import concurrent.futures
import logging as _logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel


from scripts import storage
from scripts.ssrf import is_ssrf_url as _is_ssrf_url
from scripts import applications as app_store
from scripts import user_audit
from scripts import email_verification as ev
from scripts import agent_runs
from routers.applications import VALID_STATUSES, VALID_PRIORITIES

router = APIRouter(prefix="/api/admin", tags=["admin"])
_log = _logging.getLogger(__name__)

VALID_ROLES = {"user", "admin"}


# ---------------------------------------------------------------------------
# Auth guard — imported lazily to avoid circular import
# ---------------------------------------------------------------------------

def _admin(request: Request) -> dict:
    from api import _require_admin  # noqa: PLC0415
    return _require_admin(request)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class RoleUpdate(BaseModel):
    role: str


class UserUpdate(BaseModel):
    display_name:   str | None = None
    email:          str | None = None
    role:           str | None = None
    email_verified: bool | None = None
    active:         bool | None = None


class CommentCreate(BaseModel):
    text: str

class CommentUpdate(BaseModel):
    text: str

class AppUpdate(BaseModel):
    company: str | None = None
    domain: str | None = None
    role_title: str | None = None
    status: str | None = None
    priority: str | None = None
    date_applied: str | None = None
    dua: bool | None = None
    job_source: str | None = None
    location: str | None = None
    salary_range: str | None = None
    recruiter_name: str | None = None
    recruiter_email: str | None = None
    url: str | None = None


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

@router.get("/users")
async def list_users(request: Request):
    admin = _admin(request)
    users = storage.list_all_users()

    def _enrich(u):
        uid = u["user_id"]
        try:
            apps = app_store.list_applications(uid)
            app_count = apps["total"]
        except Exception:
            _log.warning("list_users: failed to fetch application count for user %s", uid, exc_info=True)
            app_count = 0
        return {
            "user_id":        uid,
            "email":          u.get("email", ""),
            "display_name":   u.get("display_name", ""),
            "role":           u.get("role", "user"),
            "active":         u.get("active", True),
            "email_verified": u.get("email_verified", True),
            "created_at":     u.get("created_at", ""),
            "last_login":     user_audit.get_last_login(uid),
            "app_count":      app_count,
            "has_resume":     storage.has_resume(uid),
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        result = list(pool.map(_enrich, users))

    result.sort(key=lambda u: u.get("created_at", ""))
    return result


@router.put("/users/{user_id}")
async def update_user(user_id: str, body: UserUpdate, request: Request):
    """Edit display name, email, role, email_verified, or active status."""
    admin = _admin(request)
    user  = storage.get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")

    changes: dict = {}

    if body.display_name is not None:
        changes["display_name"] = {"from": user.get("display_name"), "to": body.display_name}
        user["display_name"] = body.display_name.strip()

    if body.email is not None:
        new_email = body.email.strip().lower()
        if new_email != user.get("email", ""):
            # Check email not already taken
            if storage.get_user_by_email(new_email):
                raise HTTPException(400, "That email is already in use by another account")
            changes["email"] = {"from": user.get("email"), "to": new_email}
            storage.update_user_email(user, new_email)
            # user dict now has updated email; skip save_user below for email field
            user["email"] = new_email

    if body.role is not None:
        if body.role not in VALID_ROLES:
            raise HTTPException(400, f"role must be one of: {', '.join(sorted(VALID_ROLES))}")
        changes["role"] = {"from": user.get("role", "user"), "to": body.role}
        user["role"] = body.role

    if body.email_verified is not None:
        changes["email_verified"] = {"from": user.get("email_verified"), "to": body.email_verified}
        user["email_verified"] = body.email_verified

    if body.active is not None:
        changes["active"] = {"from": user.get("active", True), "to": body.active}
        user["active"] = body.active

    storage.save_user(user)
    # Invalidate cached user record so role/active changes take effect immediately
    if changes:
        try:
            from api import _invalidate_user_cache
            _invalidate_user_cache(user_id)
        except Exception:
            pass
    user_audit.log(user_id, "admin_user_updated", admin["email"],
                   changes=changes, changed_by=admin["email"])

    return {
        "ok":             True,
        "user_id":        user_id,
        "email":          user.get("email"),
        "display_name":   user.get("display_name"),
        "role":           user.get("role", "user"),
        "active":         user.get("active", True),
        "email_verified": user.get("email_verified", True),
    }


@router.post("/users/{user_id}/resend-verification")
async def admin_resend_verification(user_id: str, request: Request):
    admin = _admin(request)
    user  = storage.get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user.get("email_verified", True):
        return {"ok": True, "already_verified": True}

    from api import _send_verification_email  # noqa: PLC0415
    token = ev.create_token(user_id, user["email"])
    sent  = _send_verification_email(user["email"], user.get("display_name", "there"), token)
    user_audit.log(user_id, "admin_verification_resent", admin["email"],
                   sent=sent, target=user["email"])
    return {"ok": True, "sent": sent}


# Keep the old role-only endpoint for backward compat with Slack bot
@router.put("/users/{user_id}/role")
async def set_user_role(user_id: str, body: RoleUpdate, request: Request):
    admin = _admin(request)
    if body.role not in VALID_ROLES:
        raise HTTPException(400, f"role must be one of: {', '.join(sorted(VALID_ROLES))}")
    user = storage.get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")
    old_role = user.get("role", "user")
    user["role"] = body.role
    storage.save_user(user)
    from api import _invalidate_user_cache
    _invalidate_user_cache(user_id)
    user_audit.log(user_id, "role_changed", admin["email"],
                   old_role=old_role, new_role=body.role, changed_by=admin["email"])
    return {"ok": True, "user_id": user_id, "role": body.role}


# ---------------------------------------------------------------------------
# Cross-user application access
# ---------------------------------------------------------------------------

@router.get("/applications")
async def list_all_applications(request: Request):
    """Return all applications across all users with a '_user_email' field added."""
    _admin(request)
    users = storage.list_all_users()

    def _fetch(u):
        uid = u["user_id"]
        try:
            result = app_store.list_applications(uid)
            items  = result.get("items", result) if isinstance(result, dict) else result
            for item in items:
                item["_user_id"]    = uid
                item["_user_email"] = u.get("email", "")
            return items
        except Exception:
            _log.warning("list_all_applications: failed to fetch applications for user %s", uid, exc_info=True)
            return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        batches = list(pool.map(_fetch, users))

    all_apps = [app for batch in batches for app in batch]
    all_apps.sort(key=lambda a: a.get("last_updated", ""), reverse=True)
    return all_apps


@router.get("/users/{user_id}/applications")
async def list_user_applications(user_id: str, request: Request):
    _admin(request)
    if not storage.get_user_by_id(user_id):
        raise HTTPException(404, "User not found")
    result = app_store.list_applications(user_id)
    return result.get("items", result) if isinstance(result, dict) else result


@router.get("/applications/{user_id}/{app_id}")
async def get_application(user_id: str, app_id: str, request: Request):
    admin = _admin(request)
    record = app_store.get_application(user_id, app_id)
    if not record:
        raise HTTPException(404, "Application not found")
    return record


@router.put("/applications/{user_id}/{app_id}")
async def update_application(user_id: str, app_id: str, body: AppUpdate, request: Request):
    from api import _client_ip  # deferred: api.py imports this router at module load time
    admin = _admin(request)
    record = app_store.get_application(user_id, app_id)
    if not record:
        raise HTTPException(404, "Application not found")

    if body.url and not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must start with http:// or https://")
    if body.status is not None and body.status not in VALID_STATUSES:
        raise HTTPException(400, f"status must be one of: {', '.join(sorted(VALID_STATUSES))}")
    if body.priority is not None and body.priority not in VALID_PRIORITIES:
        raise HTTPException(400, f"priority must be one of: {', '.join(sorted(VALID_PRIORITIES))}")

    updates = body.model_dump(exclude_unset=True)
    record.update(updates)
    record["updated_at"] = _now()
    record["updated_by"] = admin["email"]
    record.setdefault("audit_log", []).append({
        "id":        __import__("uuid").uuid4().__str__(),
        "action":    "admin_updated",
        "actor":     admin["email"],
        "timestamp": _now(),
        "ip":        _client_ip(request),
        "changes":   {k: updates[k] for k in updates},
    })

    record = app_store.save_application(user_id, record)
    user_audit.log(user_id, "admin_updated", admin["email"],
                   app_id=app_id, fields=list(updates.keys()))
    return record


@router.delete("/applications/{user_id}/{app_id}", status_code=204)
async def delete_application(user_id: str, app_id: str, request: Request):
    from api import _client_ip  # deferred: api.py imports this router at module load time
    admin = _admin(request)
    record = app_store.get_application(user_id, app_id)
    if not record:
        raise HTTPException(404, "Application not found")

    record.setdefault("audit_log", []).append({
        "id":        __import__("uuid").uuid4().__str__(),
        "action":    "admin_deleted",
        "actor":     admin["email"],
        "timestamp": _now(),
        "ip":        _client_ip(request),
        "changes":   None,
    })
    app_store.save_deleted_tombstone(user_id, record)
    app_store.delete_application(user_id, app_id)
    user_audit.log(user_id, "admin_deleted", admin["email"],
                   app_id=app_id, company=record.get("company"),
                   role_title=record.get("role_title"))


# ---------------------------------------------------------------------------
# Admin comment management (any user's application)
# ---------------------------------------------------------------------------

def _get_app_or_404(user_id: str, app_id: str) -> dict:
    record = app_store.get_application(user_id, app_id)
    if not record:
        raise HTTPException(404, "Application not found")
    return record


@router.post("/applications/{user_id}/{app_id}/comments", status_code=201)
async def add_comment(user_id: str, app_id: str, body: CommentCreate, request: Request):
    from api import _client_ip  # deferred: api.py imports this router at module load time
    admin  = _admin(request)
    record = _get_app_or_404(user_id, app_id)

    if not body.text.strip():
        raise HTTPException(400, "Comment text cannot be empty")

    now = _now()
    comment = {
        "id":         str(uuid.uuid4()),
        "text":       body.text.strip(),
        "created_at": now,
        "updated_at": now,
        "author":     admin["email"],
    }
    record.setdefault("comments", []).append(comment)
    record.setdefault("audit_log", []).append({
        "id": str(uuid.uuid4()), "action": "admin_comment_added",
        "actor": admin["email"], "timestamp": now, "ip": _client_ip(request),
        "details": {"preview": comment["text"][:60]},
    })
    record["updated_at"] = now
    record["updated_by"] = admin["email"]
    app_store.save_application(user_id, record)
    user_audit.log(user_id, "admin_comment_added", admin["email"],
                   app_id=app_id, comment_id=comment["id"],
                   preview=comment["text"][:60])
    return comment


@router.put("/applications/{user_id}/{app_id}/comments/{comment_id}")
async def update_comment(user_id: str, app_id: str, comment_id: str,
                         body: CommentUpdate, request: Request):
    from api import _client_ip  # deferred: api.py imports this router at module load time
    admin  = _admin(request)
    record = _get_app_or_404(user_id, app_id)

    if not body.text.strip():
        raise HTTPException(400, "Comment text cannot be empty")

    for c in record.get("comments", []):
        if c.get("id") == comment_id:
            old_text   = c["text"]
            c["text"]       = body.text.strip()
            c["updated_at"] = _now()
            record.setdefault("audit_log", []).append({
                "id": str(uuid.uuid4()), "action": "admin_comment_edited",
                "actor": admin["email"], "timestamp": _now(), "ip": _client_ip(request),
                "details": {"from": old_text[:60], "to": c["text"][:60]},
            })
            record["updated_at"] = _now()
            record["updated_by"] = admin["email"]
            app_store.save_application(user_id, record)
            user_audit.log(user_id, "admin_comment_edited", admin["email"],
                           app_id=app_id, comment_id=comment_id)
            return c

    raise HTTPException(404, "Comment not found")


@router.delete("/applications/{user_id}/{app_id}/comments/{comment_id}", status_code=204)
async def delete_comment(user_id: str, app_id: str, comment_id: str, request: Request):
    from api import _client_ip  # deferred: api.py imports this router at module load time
    admin  = _admin(request)
    record = _get_app_or_404(user_id, app_id)

    before = len(record.get("comments", []))
    record["comments"] = [c for c in record.get("comments", []) if c.get("id") != comment_id]
    if len(record["comments"]) == before:
        raise HTTPException(404, "Comment not found")

    record.setdefault("audit_log", []).append({
        "id": str(uuid.uuid4()), "action": "admin_comment_deleted",
        "actor": admin["email"], "timestamp": _now(), "ip": _client_ip(request),
        "details": {"comment_id": comment_id},
    })
    record["updated_at"] = _now()
    record["updated_by"] = admin["email"]
    app_store.save_application(user_id, record)
    user_audit.log(user_id, "admin_comment_deleted", admin["email"],
                   app_id=app_id, comment_id=comment_id)


# ---------------------------------------------------------------------------
# Active in-memory run listing
# ---------------------------------------------------------------------------

@router.get("/runs")
async def list_all_runs(request: Request):
    """Return ALL agent runs across all users from the agent_runs store.
    Overlays in-memory status for any currently active runs."""
    _admin(request)

    all_runs = agent_runs.list_all()

    # Overlay live in-memory status for active runs
    from api import _runs, _preps, _optimizations, _app_questions, _thank_yous  # noqa: PLC0415
    in_memory = {}
    for store in (_runs, _preps, _optimizations, _app_questions, _thank_yous):
        for rid, entry in list(store.items()):
            in_memory[rid] = entry.get("status", "running")

    for run in all_runs:
        mem_status = in_memory.get(run["id"])
        if mem_status and mem_status not in ("done", "error"):
            run["status"] = mem_status

    # Normalize field names for frontend compatibility
    for run in all_runs:
        run.setdefault("created_at", run.get("started_at", ""))
        run.setdefault("web_view_link", run.get("gdrive_folder_url", ""))
        run.setdefault("app_company", run.get("company", ""))
        run.setdefault("app_role", run.get("role", ""))

    return all_runs

# ---------------------------------------------------------------------------
# Unified audit log
# ---------------------------------------------------------------------------

# All known action types across both audit streams
AUDIT_ACTION_TYPES = sorted([
    # Auth / account
    "user_registered", "user_registered_google", "google_account_linked",
    "login_success", "login_google", "login_failed", "logout",
    "email_verified", "verification_email_resent",
    "password_reset_requested", "password_reset_completed",
    "email_changed",
    "profile_updated", "resume_uploaded", "password_changed",
    # Agent runs
    "run_started", "run_completed", "run_failed",
    "prep_started", "prep_completed", "prep_failed",
    "aq_started", "aq_completed", "aq_failed",
    "optimize_started", "optimize_completed", "optimize_failed",
    "thankyou_started", "thankyou_completed", "thankyou_failed",
    "file_downloaded",
    # Notifications
    "notification_sent",
    # Admin exports
    "admin_csv_export",
    # Webhooks
    "webhook_created", "webhook_updated", "webhook_deleted", "webhook_tested",
    # Admin user management
    "role_changed", "admin_user_updated", "admin_verification_resent",
    # Applications
    "created", "updated", "deleted",
    "comment_added", "comment_edited", "comment_deleted",
    "run_linked", "run_unlinked",
    "match_scored",
    "jd_extracted", "setup_folder_started",
    # Calendar
    "calendar_event_created", "calendar_event_updated", "calendar_event_deleted",
    # Admin application management
    "admin_updated", "admin_deleted",
    "admin_comment_added", "admin_comment_edited", "admin_comment_deleted",
    # Import
    "imported",
])


@router.get("/audit/action-types")
async def get_action_types(request: Request):
    """Return the list of known audit action types."""
    _admin(request)
    return AUDIT_ACTION_TYPES


@router.get("/audit")
async def get_unified_audit_log(
    request: Request,
    page:       int          = Query(1, ge=1),
    per_page:   int          = Query(50, ge=1, le=500),
    action:     str | None   = None,
    actor:      str | None   = None,
    user_id:    str | None   = None,
    source:     str | None   = None,
    event_id:   str | None   = None,
    from_ts:    str | None   = None,
    to_ts:      str | None   = None,
    sort_order: str          = Query("desc", pattern="^(asc|desc)$"),
):
    """Unified audit log across all users and all application records."""
    _admin(request)

    users       = storage.list_all_users()
    user_map    = {u["user_id"]: u.get("email", "") for u in users}
    all_events: list[dict[str, Any]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        # ── 1. User-level audit events ──────────────────────────────
        if source in (None, "user"):
            def _user_events(u):
                uid   = u["user_id"]
                email = u.get("email", "")
                try:
                    return [{**e, "source": "user", "user_id": uid,
                             "user_email": email, "entity_id": None,
                             "entity_label": None}
                            for e in user_audit.get_events(uid)]
                except Exception:
                    _log.warning("audit log: failed to fetch user events for %s", uid, exc_info=True)
                    return []
            for batch in pool.map(_user_events, users):
                all_events.extend(batch)

        # ── 2. Application-level audit events ───────────────────────
        if source in (None, "application"):
            def _app_events(u):
                uid = u["user_id"]
                events = []
                try:
                    result = app_store.list_applications(uid)
                    items  = result.get("items", result) if isinstance(result, dict) else result
                    for item in items:
                        try:
                            full = app_store.get_application(uid, item["id"])
                            if not full:
                                continue
                            label = f"{full.get('company','')} · {full.get('role_title','')}"
                            for e in full.get("audit_log", []):
                                events.append({
                                    **e, "source": "application",
                                    "user_id": uid,
                                    "user_email": user_map.get(uid, ""),
                                    "entity_id": item["id"],
                                    "entity_label": label,
                                    "actor": e.get("actor", ""),
                                    "ip": e.get("ip"),
                                    "details": e.get("changes") or e.get("details") or {},
                                })
                        except Exception:
                            _log.warning("audit log: failed to fetch application %s for user %s",
                                        item.get("id"), uid, exc_info=True)
                except Exception:
                    _log.warning("audit log: failed to list applications for user %s", uid, exc_info=True)
                return events
            for batch in pool.map(_app_events, users):
                all_events.extend(batch)

    # ── 3. Filter ───────────────────────────────────────────────────
    if action:
        action_set = set(a.strip() for a in action.split(",") if a.strip())
        all_events = [e for e in all_events if e.get("action") in action_set]
    if actor:
        lc = actor.lower()
        all_events = [e for e in all_events if lc in (e.get("actor") or "").lower()]
    if user_id:
        all_events = [e for e in all_events if e.get("user_id") == user_id]
    if event_id:
        lc = event_id.lower()
        all_events = [e for e in all_events if lc in (e.get("id") or "").lower()]
    if from_ts:
        all_events = [e for e in all_events if (e.get("timestamp") or "") >= from_ts]
    if to_ts:
        all_events = [e for e in all_events if (e.get("timestamp") or "") <= to_ts]

    # ── 4. Sort ─────────────────────────────────────────────────────
    all_events.sort(
        key=lambda e: e.get("timestamp") or "",
        reverse=(sort_order == "desc"),
    )

    # ── 5. Paginate ──────────────────────────────────────────────────
    total  = len(all_events)
    pages  = max(1, (total + per_page - 1) // per_page)
    page   = max(1, min(page, pages))
    start  = (page - 1) * per_page
    items  = all_events[start : start + per_page]

    return {
        "items":    items,
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    pages,
    }


# ---------------------------------------------------------------------------
# Audit log export (no pagination — returns full filtered result)
# ---------------------------------------------------------------------------

@router.get("/audit/export")
async def export_audit_log(
    request: Request,
    action:     str | None = None,
    actor:      str | None = None,
    user_id:    str | None = None,
    source:     str | None = None,
    event_id:   str | None = None,
    from_ts:    str | None = None,
    to_ts:      str | None = None,
    sort_order: str        = Query("desc", pattern="^(asc|desc)$"),
):
    """Return all matching audit events without pagination (for CSV export)."""
    admin = _admin(request)

    users    = storage.list_all_users()
    user_map = {u["user_id"]: u.get("email", "") for u in users}
    all_events: list[dict[str, Any]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        def _user_ev(u):
            uid, email = u["user_id"], u.get("email", "")
            try:
                return [{**e, "source": "user", "user_id": uid,
                         "user_email": email, "entity_id": None, "entity_label": None}
                        for e in user_audit.get_events(uid)]
            except Exception:
                _log.warning("audit export: failed to fetch user events for %s", uid, exc_info=True)
                return []

        def _app_ev(u):
            uid, events = u["user_id"], []
            try:
                result = app_store.list_applications(uid)
                items  = result.get("items", result) if isinstance(result, dict) else result
                for item in items:
                    try:
                        full = app_store.get_application(uid, item["id"])
                        if not full:
                            continue
                        label = f"{full.get('company','')} · {full.get('role_title','')}"
                        for e in full.get("audit_log", []):
                            events.append({**e, "source": "application",
                                           "user_id": uid, "user_email": user_map.get(uid, ""),
                                           "entity_id": item["id"], "entity_label": label,
                                           "actor": e.get("actor", ""), "ip": e.get("ip"),
                                           "details": e.get("changes") or e.get("details") or {}})
                    except Exception:
                        _log.warning("audit export: failed to fetch application %s for user %s",
                                    item.get("id"), uid, exc_info=True)
            except Exception:
                _log.warning("audit export: failed to list applications for user %s", uid, exc_info=True)
            return events

        for batch in pool.map(_user_ev, users):
            all_events.extend(batch)
        for batch in pool.map(_app_ev, users):
            all_events.extend(batch)

    if action:    all_events = [e for e in all_events if e.get("action") == action]
    if actor:     all_events = [e for e in all_events if actor.lower() in (e.get("actor") or "").lower()]
    if user_id:   all_events = [e for e in all_events if e.get("user_id") == user_id]
    if source:    all_events = [e for e in all_events if e.get("source") == source]
    if event_id:  all_events = [e for e in all_events if event_id.lower() in (e.get("id") or "").lower()]
    if from_ts:   all_events = [e for e in all_events if (e.get("timestamp") or "") >= from_ts]
    if to_ts:     all_events = [e for e in all_events if (e.get("timestamp") or "") <= to_ts]

    all_events.sort(key=lambda e: e.get("timestamp") or "", reverse=(sort_order == "desc"))

    user_audit.log(admin["user_id"], "admin_csv_export", admin["email"],
                   screen="audit_log", row_count=len(all_events),
                   filters={"action": action, "actor": actor, "source": source,
                            "from_ts": from_ts, "to_ts": to_ts})

    return all_events


# ---------------------------------------------------------------------------
# Generic activity log (for client-side export events)
# ---------------------------------------------------------------------------

class ActivityEntry(BaseModel):
    screen:    str
    row_count: int
    filters:   dict[str, Any] = {}


@router.post("/log-activity")
async def log_activity(body: ActivityEntry, request: Request):
    """Log an admin action (e.g. CSV export) originating from the browser."""
    from api import _client_ip  # deferred: api.py imports this router at module load time

    admin = _admin(request)
    user_audit.log(admin["user_id"], "admin_csv_export", admin["email"],
                   _client_ip(request),
                   screen=body.screen, row_count=body.row_count, filters=body.filters)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

from scripts import webhooks as wh_store  # noqa: E402


VALID_PAYLOAD_FORMATS = {"generic", "slack", "ms_teams", "grafana_loki"}


VALID_FILTER_CATEGORIES = {"auth", "profile", "applications", "runs", "admin"}


class WebhookCreate(BaseModel):
    name:               str
    url:                str
    events:             list[str] = ["*"]
    headers:            dict[str, str] = {}
    query_params:       dict[str, str] = {}
    secret:             str = ""
    active:             bool = True
    payload_format:     str = "generic"
    filter_actors:      str = ""            # comma-separated emails/user_ids
    filter_source:      str = ""            # "" | "user" | "application"
    filter_categories:  list[str] = []      # subset of VALID_FILTER_CATEGORIES
    filter_app_id:      str = ""


class WebhookUpdate(BaseModel):
    name:               str | None = None
    url:                str | None = None
    events:             list[str] | None = None
    headers:            dict[str, str] | None = None
    query_params:       dict[str, str] | None = None
    secret:             str | None = None
    active:             bool | None = None
    payload_format:     str | None = None
    filter_actors:      str | None = None
    filter_source:      str | None = None
    filter_categories:  list[str] | None = None
    filter_app_id:      str | None = None


@router.get("/webhooks")
async def list_webhooks(request: Request):
    _admin(request)
    return wh_store.list_webhooks()


@router.post("/webhooks", status_code=201)
async def create_webhook(body: WebhookCreate, request: Request):
    admin = _admin(request)
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must start with http:// or https://")
    if _is_ssrf_url(body.url):
        raise HTTPException(400, "url must not point to a private or internal network address")
    if body.payload_format not in VALID_PAYLOAD_FORMATS:
        raise HTTPException(400, f"payload_format must be one of: {', '.join(sorted(VALID_PAYLOAD_FORMATS))}")
    if body.filter_categories:
        bad = [c for c in body.filter_categories if c not in VALID_FILTER_CATEGORIES]
        if bad:
            raise HTTPException(400, f"Unknown filter_categories: {bad}. Valid: {sorted(VALID_FILTER_CATEGORIES)}")
    webhook = {
        "id":               str(uuid.uuid4()),
        "name":             body.name.strip(),
        "url":              body.url.strip(),
        "events":           body.events,
        "headers":          body.headers,
        "query_params":     body.query_params,
        "secret":             body.secret,
        "active":             body.active,
        "payload_format":     body.payload_format,
        "filter_actors":      body.filter_actors,
        "filter_source":      body.filter_source,
        "filter_categories":  body.filter_categories,
        "filter_app_id":      body.filter_app_id,
        "created_at":       _now(),
        "created_by":       admin["email"],
        "last_triggered_at": None,
        "delivery_stats":   {"total": 0, "success": 0, "failure": 0},
        "recent_deliveries": [],
    }
    wh_store.save_webhook(webhook)
    user_audit.log(admin["user_id"], "webhook_created", admin["email"],
                   webhook_id=webhook["id"], name=webhook["name"])
    return _redact_webhook_secret(webhook)


def _redact_webhook_secret(w: dict) -> dict:
    """Return a copy of the webhook record with the secret redacted for API responses."""
    out = dict(w)
    raw = out.get("secret") or ""
    if len(raw) > 8:
        out["secret"] = raw[:4] + "*" * (len(raw) - 8) + raw[-4:]
    elif raw:
        out["secret"] = "****"
    return out


@router.get("/webhooks/{webhook_id}")
async def get_webhook(webhook_id: str, request: Request):
    _admin(request)
    w = wh_store.get_webhook(webhook_id)
    if not w:
        raise HTTPException(404, "Webhook not found")
    return _redact_webhook_secret(w)


@router.put("/webhooks/{webhook_id}")
async def update_webhook(webhook_id: str, body: WebhookUpdate, request: Request):
    admin = _admin(request)
    w = wh_store.get_webhook(webhook_id)
    if not w:
        raise HTTPException(404, "Webhook not found")
    if body.url is not None and not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must start with http:// or https://")
    if body.url is not None and _is_ssrf_url(body.url):
        raise HTTPException(400, "url must not point to a private or internal network address")
    if body.filter_categories is not None:
        bad = [c for c in body.filter_categories if c not in VALID_FILTER_CATEGORIES]
        if bad:
            raise HTTPException(400, f"Unknown filter_categories: {bad}. Valid: {sorted(VALID_FILTER_CATEGORIES)}")
    for field, val in body.model_dump(exclude_unset=True).items():
        w[field] = val
    wh_store.save_webhook(w)
    user_audit.log(admin["user_id"], "webhook_updated", admin["email"],
                   webhook_id=webhook_id, changes=body.model_dump(exclude_unset=True))
    return _redact_webhook_secret(w)


@router.delete("/webhooks/{webhook_id}", status_code=204)
async def delete_webhook(webhook_id: str, request: Request):
    admin = _admin(request)
    if not wh_store.delete_webhook(webhook_id):
        raise HTTPException(404, "Webhook not found")
    user_audit.log(admin["user_id"], "webhook_deleted", admin["email"],
                   webhook_id=webhook_id)


@router.post("/webhooks/{webhook_id}/test")
async def test_webhook(webhook_id: str, request: Request):
    from api import _client_ip  # deferred: api.py imports this router at module load time
    admin = _admin(request)
    w = wh_store.get_webhook(webhook_id)
    if not w:
        raise HTTPException(404, "Webhook not found")
    test_event = {
        "id":         str(uuid.uuid4()),
        "action":     "webhook_tested",
        "actor":      admin["email"],
        "timestamp":  _now(),
        "ip":         _client_ip(request),
        "details":    {"message": "This is a test delivery from Job Apply admin."},
        "user_id":    admin["user_id"],
        "user_email": admin["email"],
    }
    # Deliver synchronously so we can return the result
    import threading as _th  # noqa: PLC0415
    result: dict = {}
    def _run():
        from scripts.webhooks import _deliver  # noqa: PLC0415
        _deliver(w, test_event)
        fresh = wh_store.get_webhook(webhook_id)
        if fresh and fresh.get("recent_deliveries"):
            result["delivery"] = fresh["recent_deliveries"][0]
    t = _th.Thread(target=_run); t.start(); t.join(timeout=15)
    user_audit.log(admin["user_id"], "webhook_tested", admin["email"],
                   webhook_id=webhook_id, webhook_name=w.get("name"))
    return result.get("delivery", {"success": None, "error": "timeout"})


@router.get("/webhooks/{webhook_id}/deliveries")
async def get_deliveries(webhook_id: str, request: Request):
    _admin(request)
    w = wh_store.get_webhook(webhook_id)
    if not w:
        raise HTTPException(404, "Webhook not found")
    deliveries = w.get("recent_deliveries", [])
    # Sort descending by timestamp and enforce the cap server-side
    deliveries = sorted(deliveries, key=lambda d: d.get("timestamp", ""), reverse=True)
    return deliveries[:wh_store._MAX_DELIVERIES]
