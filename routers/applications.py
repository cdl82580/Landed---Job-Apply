"""
routers/applications.py — CRUD endpoints for job application tracking.

Endpoints:
  GET    /api/applications                         list (with optional ?status= ?priority= filters)
  POST   /api/applications                         create
  GET    /api/applications/{app_id}                get full record
  PUT    /api/applications/{app_id}                update
  DELETE /api/applications/{app_id}                delete
  GET    /api/applications/{app_id}/audit          audit log for one application
  POST   /api/applications/{app_id}/comments       add comment
  PUT    /api/applications/{app_id}/comments/{cid} edit comment
  DELETE /api/applications/{app_id}/comments/{cid} delete comment
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from queue import Empty, Queue
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from scripts import applications as app_store
from scripts import notif_dispatch, user_audit

import logging as _logging
_log = _logging.getLogger(__name__)

router = APIRouter(prefix="/api/applications", tags=["applications"])

FLY_MACHINE_ID = os.environ.get("FLY_MACHINE_ID", "")

VALID_STATUSES = {
    "Not Applying", "Researching", "Applied", "Phone Screen",
    "Interviewing", "On Hold", "Offer", "Rejected", "No Response",
}
VALID_PRIORITIES = {"Low", "Medium", "High"}

# Fields excluded from change-diff (internal / not user-editable)
_AUDIT_SKIP = {"last_updated", "updated_by", "audit_log", "comments"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class ApplicationCreate(BaseModel):
    company: str
    domain: str = ""
    company_logo_url: str = ""
    role_title: str
    status: str = "Researching"
    date_applied: str | None = None
    dua: bool = False
    job_source: str = ""
    location: str = ""
    salary_range: str = ""
    priority: str = "Medium"
    recruiter_name: str = ""
    recruiter_email: str = ""
    url: str = ""


class ApplicationUpdate(BaseModel):
    company: str | None = None
    domain: str | None = None
    company_logo_url: str | None = None
    role_title: str | None = None
    status: str | None = None
    date_applied: str | None = None
    dua: bool | None = None
    job_source: str | None = None
    location: str | None = None
    salary_range: str | None = None
    priority: str | None = None
    recruiter_name: str | None = None
    recruiter_email: str | None = None
    url: str | None = None


class CommentCreate(BaseModel):
    text: str


class CommentUpdate(BaseModel):
    text: str


class RunLinkCreate(BaseModel):
    type: str             # "resume" | "interview_prep"
    folder_name: str
    folder_url: str = ""
    gdrive_folder_id: str = ""


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------

def _audit_entry(
    action: str,
    actor: str,
    changes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id":        str(uuid.uuid4()),
        "action":    action,
        "actor":     actor,
        "timestamp": _now(),
        "changes":   changes,
    }


def _diff(old: dict, new: dict) -> dict[str, Any]:
    """Return {field: {from: old_val, to: new_val}} for fields that changed."""
    changes = {}
    all_keys = set(old) | set(new)
    for k in all_keys:
        if k in _AUDIT_SKIP:
            continue
        ov, nv = old.get(k), new.get(k)
        if ov != nv:
            changes[k] = {"from": ov, "to": nv}
    return changes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_404(user_id: str, app_id: str) -> dict:
    record = app_store.get_application(user_id, app_id)
    if not record:
        raise HTTPException(404, "Application not found")
    return record


def _actor(request: Request) -> str:
    return request.state.user.get("email", request.state.user.get("user_id", "unknown"))


def _app_gdrive_folder_id(record: dict) -> str | None:
    """The Drive folder backing this application's resumes. All runs for a
    company/role share one folder, so prefer the most recently linked
    resume/optimize run, then fall back to any linked run with a folder id."""
    runs = [r for r in (record.get("linked_runs") or []) if r.get("gdrive_folder_id")]
    if not runs:
        return None
    runs.sort(key=lambda r: r.get("linked_at", ""), reverse=True)
    preferred = next((r for r in runs if r.get("type") in ("resume", "optimize")), None)
    return (preferred or runs[0])["gdrive_folder_id"]


def _run_match_scoring(
    user_id: str, app_id: str, jd_text: str, actor: str, scored_by: str,
) -> dict:
    """Score the user's resume/profile against jd_text, persist the result on the
    application record, and write audit entries. Returns the match_score dict.

    Scores the most recent tailored resume in the application's Drive folder when
    one exists, falling back to the user's master resume otherwise. Raises
    ValueError when the user has no resume/profile, LookupError when the
    application record is gone."""
    import tempfile
    from pathlib import Path

    from apply import (WorkflowConfig, extract_resume_text,
                       get_latest_gdrive_resume_text, score_application_match)
    from scripts import storage

    profile_text = storage.get_profile(user_id)
    if not profile_text:
        raise ValueError("No profile guide saved. Add one in your profile.")

    config = WorkflowConfig(progress=lambda _: None, user_label=actor)

    # Prefer the most recent tailored resume in this application's Drive folder;
    # fall back to the user's master resume when no run has produced one yet.
    resume_text = None
    record = app_store.get_application(user_id, app_id)
    folder_id = _app_gdrive_folder_id(record) if record else None
    if folder_id:
        resume_text = get_latest_gdrive_resume_text(folder_id, config)

    if not resume_text:
        resume_bytes = storage.get_resume(user_id)
        if not resume_bytes:
            raise ValueError("No master resume uploaded. Add one in your profile.")
        tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False, dir="/tmp")
        try:
            tmp.write(resume_bytes)
            tmp.close()
            resume_config = WorkflowConfig(progress=lambda _: None, user_label=actor,
                                           master_resume=Path(tmp.name))
            resume_text = extract_resume_text(resume_config)
        finally:
            try:
                Path(tmp.name).unlink()
            except OSError:
                pass

    match_score = score_application_match(jd_text, resume_text, profile_text, config)
    match_score["scored_at"] = _now()
    match_score["scored_by"] = scored_by

    record = app_store.save_match_score(user_id, app_id, match_score)
    if not record:
        raise LookupError("Application not found")
    record.setdefault("audit_log", []).append(
        _audit_entry("match_scored", scored_by, {
            "score": match_score["score"], "category": match_score["category"],
        })
    )
    app_store.save_application(user_id, record)
    user_audit.log(user_id, "match_scored", actor, app_id=app_id,
                   score=match_score["score"], category=match_score["category"])
    return match_score


# In-memory post-create pipeline state, keyed by app_id (single web machine —
# the create/update response pins the browser via fly-force-instance-id).
_pipelines: dict[str, dict[str, Any]] = {}
_PIPELINE_TTL_SECS = 3600


def _evict_stale_pipelines() -> None:
    cutoff = time.time() - _PIPELINE_TTL_SECS
    for app_id in [k for k, v in _pipelines.items() if v["started_at"] < cutoff]:
        _pipelines.pop(app_id, None)


def _start_application_pipeline(
    user_id: str, app_id: str, company: str, role_title: str, url: str, actor: str,
    jd_text: str = "",
    on_complete: Any = None,
) -> None:
    """Best-effort, async post-create/update pipeline:
      1. ensure the application's Drive folder exists and link it to the record
      2. extract the job description from `url` via Claude (or use `jd_text` when
         the user pasted one manually), save job_description.md
      3. score the user's resume/profile against the extracted JD
    Streams structured progress events to GET /{app_id}/pipeline/stream.
    Never raises — failures here must never affect the create/update response."""
    _log.warning("pipeline: _start_application_pipeline called app_id=%s company=%s", app_id, company)
    try:
        from apply import (WorkflowConfig, ensure_application_gdrive_folder,
                           extract_job_description_from_url, safe_filename,
                           save_gdrive_job_posting)

        _evict_stale_pipelines()
        q: Queue[dict | None] = Queue()
        _pipelines[app_id] = {"queue": q, "user_id": user_id, "started_at": time.time()}

        run_id = str(uuid.uuid4())
        user_audit.log(user_id, "jd_capture_started", actor,
                       run_id=run_id, company=company, role=role_title,
                       app_id=app_id)

        def emit(step: str, state: str, message: str = "", **extra: Any) -> None:
            q.put({"step": step, "state": state, "message": message, **extra})

        _log.warning("pipeline: thread launching app_id=%s company=%s role=%s", app_id, company, role_title)

        def _run():
            _log.warning("pipeline: thread started app_id=%s", app_id)
            folder_url = ""
            score_payload = None
            try:
                config = WorkflowConfig(
                    progress=lambda line: q.put({"log": str(line).strip()}),
                    user_label=actor,
                )

                # Stage 1 — Drive folder
                _log.warning("pipeline: stage1 starting app_id=%s", app_id)
                emit("folder", "running", "Creating Google Drive folder…")
                folder = ensure_application_gdrive_folder(company, role_title, config)
                if not folder:
                    emit("folder", "failed", "Could not create or find the Drive folder")
                    emit("jd", "skipped", "Skipped — no Drive folder")
                    emit("score", "skipped", "Skipped — no job description")
                    user_audit.log(user_id, "jd_capture_failed", actor,
                                   run_id=run_id, company=company, role=role_title,
                                   app_id=app_id, error="drive folder unresolved")
                    return
                folder_id, folder_url = folder
                folder_name = f"{safe_filename(company)}_{safe_filename(role_title)}"
                # Re-runs (manual JD paste, URL edits) must not duplicate the link
                existing = app_store.get_application(user_id, app_id) or {}
                already_linked = any(
                    r.get("type") == "job_description" and r.get("gdrive_folder_id") == folder_id
                    for r in existing.get("linked_runs", [])
                )
                if not already_linked:
                    record = app_store.link_run(user_id, app_id, {
                        "id":               str(uuid.uuid4()),
                        "type":             "job_description",
                        "folder_name":      folder_name,
                        "folder_url":       folder_url,
                        "gdrive_folder_id": folder_id,
                        "linked_at":        _now(),
                        "linked_by":        "system",
                    })
                    if record:
                        record.setdefault("audit_log", []).append(
                            _audit_entry("run_linked", "system", {
                                "type": "job_description", "folder_name": folder_name,
                            })
                        )
                        app_store.save_application(user_id, record)
                emit("folder", "done", folder_name, folder_url=folder_url)

                # Stage 2 — job_description.md
                if jd_text:
                    emit("jd", "running", "Saving the pasted job description…")
                    text = jd_text
                else:
                    if not url:
                        emit("jd", "skipped", "No posting URL on this application")
                        emit("score", "skipped", "No job description to score against")
                        return
                    emit("jd", "running", "Extracting the job description…")
                    text = extract_job_description_from_url(url, config)
                if not text:
                    emit("jd", "failed", "Could not extract a job description from the posting URL")
                    emit("score", "skipped", "No job description to score against")
                    user_audit.log(user_id, "jd_capture_failed", actor,
                                   run_id=run_id, company=company, role=role_title,
                                   app_id=app_id)
                    return
                if save_gdrive_job_posting(folder_id, text, config):
                    emit("jd", "done", "job_description.md saved to Drive")
                else:
                    emit("jd", "failed", "Could not save job_description.md to Drive")
                user_audit.log(user_id, "jd_capture_completed", actor,
                               run_id=run_id, company=company, role=role_title,
                               app_id=app_id, folder_url=folder_url)

                # Stage 3 — match scoring (only once a JD exists)
                emit("score", "running", "Scoring your resume against the posting…")
                try:
                    score_payload = _run_match_scoring(
                        user_id, app_id, text, actor, scored_by="system")
                except Exception as exc:
                    emit("score", "failed", f"Scoring failed: {exc}")
                else:
                    emit("score", "done",
                         f"{score_payload['score']} — {score_payload['category']}",
                         score=score_payload["score"],
                         category=score_payload["category"],
                         rationale=score_payload.get("rationale", ""))
            except Exception as exc:
                _log.exception("pipeline: thread fatal error app_id=%s: %s", app_id, exc)
                try:
                    q.put({"fatal": str(exc)})
                    user_audit.log(user_id, "jd_capture_failed", actor,
                                   run_id=run_id, company=company, role=role_title,
                                   app_id=app_id, error=str(exc))
                except Exception:
                    pass
            finally:
                done: dict[str, Any] = {"done": True, "folder_url": folder_url}
                if score_payload:
                    done["score"] = score_payload
                q.put(done)
                q.put(None)
                if on_complete:
                    try:
                        fresh = app_store.get_application(user_id, app_id) or {}
                        on_complete(fresh)
                    except Exception:
                        _log.exception("pipeline: on_complete callback failed app_id=%s", app_id)

        threading.Thread(target=_run, daemon=True).start()
    except Exception as _exc:
        _log.exception("pipeline: failed to start for app_id=%s: %s", app_id, _exc)


# ---------------------------------------------------------------------------
# Application CRUD
# ---------------------------------------------------------------------------

@router.get("")
async def list_applications(
    request: Request,
    status:   str | None = None,
    priority: str | None = None,
    page:     int = Query(1, ge=1),
    per_page: int = Query(0, ge=0),
):
    user_id = request.state.user["user_id"]
    return app_store.list_applications(
        user_id, status=status, priority=priority, page=page, per_page=per_page
    )


@router.post("", status_code=201)
async def create_application(body: ApplicationCreate, request: Request, response: Response):
    user_id = request.state.user["user_id"]
    actor   = _actor(request)

    if body.status not in VALID_STATUSES:
        raise HTTPException(400, f"status must be one of: {', '.join(sorted(VALID_STATUSES))}")
    if body.priority not in VALID_PRIORITIES:
        raise HTTPException(400, f"priority must be one of: {', '.join(sorted(VALID_PRIORITIES))}")
    if body.url and not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must start with http:// or https://")
    if body.company_logo_url and not body.company_logo_url.startswith("https://"):
        raise HTTPException(400, "company_logo_url must start with https://")

    now = _now()
    record = {
        "id":               str(uuid.uuid4()),
        "user_id":          user_id,
        "created_at":       now,
        "created_by":       actor,
        "updated_at":       now,
        "updated_by":       actor,
        "status_changed_at": now,
        "comments":         [],
        "audit_log":        [],
        **body.model_dump(),
    }
    record["audit_log"].append(_audit_entry("created", actor))
    record = app_store.save_application(user_id, record)
    user_audit.log(user_id, "created", actor, app_id=record["id"],
                   company=record["company"], role_title=record["role_title"])

    pipeline_started = False
    if record.get("url"):
        _start_application_pipeline(
            user_id, record["id"], record["company"], record["role_title"], record["url"], actor,
            on_complete=lambda r: notif_dispatch.notify_new_application(user_id, r),
        )
        pipeline_started = True
    else:
        import threading as _threading
        _threading.Thread(
            target=notif_dispatch.notify_new_application,
            args=(user_id, record),
            daemon=True,
        ).start()
        # Pin this browser to the machine holding the pipeline's in-memory queue
        if FLY_MACHINE_ID:
            response.set_cookie("fly-force-instance-id", FLY_MACHINE_ID,
                                path="/", samesite="lax", httponly=True, secure=True)

    return {**record, "pipeline_started": pipeline_started}


@router.get("/{app_id}")
async def get_application(app_id: str, request: Request):
    return _get_or_404(request.state.user["user_id"], app_id)


@router.put("/{app_id}")
async def update_application(app_id: str, body: ApplicationUpdate, request: Request, response: Response):
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    record  = _get_or_404(user_id, app_id)

    updates = body.model_dump(exclude_unset=True)  # preserves False + explicit nulls

    if "status" in updates and updates["status"] not in VALID_STATUSES:
        raise HTTPException(400, f"status must be one of: {', '.join(sorted(VALID_STATUSES))}")
    if "priority" in updates and updates["priority"] not in VALID_PRIORITIES:
        raise HTTPException(400, f"priority must be one of: {', '.join(sorted(VALID_PRIORITIES))}")
    if updates.get("url") and not updates["url"].startswith(("http://", "https://")):
        raise HTTPException(400, "url must start with http:// or https://")
    if updates.get("company_logo_url") and not updates["company_logo_url"].startswith("https://"):
        raise HTTPException(400, "company_logo_url must start with https://")

    # Compute diff before applying
    changes = _diff(record, {**record, **updates})

    record.update(updates)
    # Auto-set applied_date when status transitions to Applied
    if updates.get("status") == "Applied" and (changes or {}).get("status", {}).get("from") != "Applied":
        if not record.get("date_applied"):
            record["date_applied"] = _now()[:10]  # YYYY-MM-DD
    if "status" in (changes or {}):
        record["status_changed_at"] = _now()
    record["updated_at"] = _now()
    record["updated_by"] = actor

    if changes:
        record.setdefault("audit_log", []).append(
            _audit_entry("updated", actor, changes)
        )

    record = app_store.save_application(user_id, record)

    if changes:
        user_audit.log(user_id, "updated", actor, app_id=app_id,
                       company=record["company"], role_title=record["role_title"],
                       fields=list(changes.keys()))

    if "status" in changes:
        old_status = changes["status"]["from"]
        new_status = changes["status"]["to"]
        import threading as _threading
        _threading.Thread(
            target=notif_dispatch.notify_status_changed,
            args=(user_id, record, old_status, new_status),
            daemon=True,
        ).start()

    # If the URL actually changed, re-capture the job description and re-score
    pipeline_started = False
    if updates.get("url") and "url" in (changes or {}):
        _start_application_pipeline(
            user_id, app_id,
            record["company"], record["role_title"], updates["url"], actor,
        )
        pipeline_started = True
        if FLY_MACHINE_ID:
            response.set_cookie("fly-force-instance-id", FLY_MACHINE_ID,
                                path="/", samesite="lax", httponly=True, secure=True)

    return {**record, "pipeline_started": pipeline_started}


@router.delete("/{app_id}", status_code=204)
async def delete_application(app_id: str, request: Request):
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    record  = _get_or_404(user_id, app_id)

    # Write one final audit entry into the record before deleting
    record.setdefault("audit_log", []).append(_audit_entry("deleted", actor))
    # Save a tombstone in a separate key so deletion is auditable
    app_store.save_deleted_tombstone(user_id, record)

    app_store.delete_application(user_id, app_id)
    user_audit.log(user_id, "deleted", actor, app_id=app_id,
                   company=record.get("company"), role_title=record.get("role_title"))


@router.get("/{app_id}/audit")
async def get_audit_log(app_id: str, request: Request):
    record = _get_or_404(request.state.user["user_id"], app_id)
    return record.get("audit_log", [])


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

@router.post("/{app_id}/comments", status_code=201)
async def add_comment(app_id: str, body: CommentCreate, request: Request):
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    record  = _get_or_404(user_id, app_id)

    if not body.text.strip():
        raise HTTPException(400, "Comment text cannot be empty")

    now = _now()
    comment = {
        "id":         str(uuid.uuid4()),
        "text":       body.text.strip(),
        "created_at": now,
        "updated_at": now,
        "author":     actor,
    }
    record.setdefault("comments", []).append(comment)
    record.setdefault("audit_log", []).append(
        _audit_entry("comment_added", actor, {"comment_id": comment["id"], "preview": comment["text"][:60]})
    )
    record["updated_at"] = now
    record["updated_by"] = actor
    app_store.save_application(user_id, record)
    user_audit.log(user_id, "comment_added", actor, app_id=app_id,
                   comment_id=comment["id"], preview=comment["text"][:60])
    return comment


@router.put("/{app_id}/comments/{comment_id}")
async def update_comment(
    app_id: str, comment_id: str, body: CommentUpdate, request: Request
):
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    record  = _get_or_404(user_id, app_id)

    if not body.text.strip():
        raise HTTPException(400, "Comment text cannot be empty")

    for c in record.get("comments", []):
        if c["id"] == comment_id:
            old_text = c["text"]
            c["text"]       = body.text.strip()
            c["updated_at"] = _now()
            record.setdefault("audit_log", []).append(
                _audit_entry("comment_edited", actor, {
                    "comment_id": comment_id,
                    "from": old_text[:60],
                    "to":   c["text"][:60],
                })
            )
            record["updated_at"] = _now()
            record["updated_by"] = actor
            app_store.save_application(user_id, record)
            user_audit.log(user_id, "comment_edited", actor, app_id=app_id,
                           comment_id=comment_id)
            return c  # comment object, not record — no need to reassign

    raise HTTPException(404, "Comment not found")


# ---------------------------------------------------------------------------
# Run links
# ---------------------------------------------------------------------------

@router.get("/{app_id}/runs")
async def get_linked_runs(app_id: str, request: Request):
    record = _get_or_404(request.state.user["user_id"], app_id)
    return record.get("linked_runs", [])


@router.post("/{app_id}/runs", status_code=201)
async def link_run(app_id: str, body: RunLinkCreate, request: Request):
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    _get_or_404(user_id, app_id)  # 404 guard

    if body.gdrive_folder_id:
        # gdrive_folder_id ends up trusted downstream (job-posting read/write,
        # optimize-run access) as proof this folder belongs to the caller, so
        # verify it against the caller's actual Drive folders here rather than
        # accepting an arbitrary client-supplied id — otherwise any user could
        # link (and thereby read/write) another user's Drive folder by guessing
        # or observing its id.
        from apply import WorkflowConfig, list_gdrive_run_folders
        config = WorkflowConfig(progress=lambda _: None, user_label=actor)
        try:
            owned_ids = {f.get("id") for f in list_gdrive_run_folders(actor, config) if f.get("id")}
        except Exception:
            raise HTTPException(503, "Could not verify Drive folder ownership — please try again")
        if body.gdrive_folder_id not in owned_ids:
            raise HTTPException(403, "That Drive folder does not belong to you")

    run_info = {
        "id":               str(uuid.uuid4()),
        "type":             body.type,
        "folder_name":      body.folder_name,
        "folder_url":       body.folder_url,
        "gdrive_folder_id": body.gdrive_folder_id,
        "linked_at":        _now(),
        "linked_by":        actor,
    }
    record = app_store.link_run(user_id, app_id, run_info)
    if not record:
        raise HTTPException(404, "Application not found")

    record.setdefault("audit_log", []).append(
        _audit_entry("run_linked", actor, {
            "run_id":      run_info["id"],
            "type":        body.type,
            "folder_name": body.folder_name,
        })
    )
    app_store.save_application(user_id, record)
    user_audit.log(user_id, "run_linked", actor, app_id=app_id,
                   run_id=run_info["id"], type=body.type, folder_name=body.folder_name)
    return run_info


@router.delete("/{app_id}/runs/{link_id}", status_code=204)
async def unlink_run(app_id: str, link_id: str, request: Request):
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    record  = _get_or_404(user_id, app_id)

    removed = app_store.unlink_run(user_id, app_id, link_id)
    if not removed:
        raise HTTPException(404, "Run link not found")

    record = app_store.get_application(user_id, app_id)
    if record:
        record.setdefault("audit_log", []).append(
            _audit_entry("run_unlinked", actor, {"link_id": link_id})
        )
        app_store.save_application(user_id, record)
        user_audit.log(user_id, "run_unlinked", actor, app_id=app_id, link_id=link_id)


def _resolve_jd_text(record: dict, config) -> str | None:
    """Find job description text for an application: prefer a Drive folder linked
    via any run (job_description capture happens into the same per-company/role
    folder as resume/interview-prep runs), falling back to extracting fresh from
    the application's posting URL."""
    from apply import get_gdrive_job_posting, extract_job_description_from_url

    seen_folders: set[str] = set()
    linked = record.get("linked_runs", [])
    # Prefer an explicit job_description link, then any other linked folder.
    ordered = sorted(linked, key=lambda r: 0 if r.get("type") == "job_description" else 1)
    for run in ordered:
        folder_id = run.get("gdrive_folder_id")
        if not folder_id or folder_id in seen_folders:
            continue
        seen_folders.add(folder_id)
        text = get_gdrive_job_posting(folder_id, config)
        if text:
            return text

    url = record.get("url")
    if url:
        return extract_job_description_from_url(url, config)
    return None


@router.post("/{app_id}/score")
async def score_application(app_id: str, request: Request):
    """(Re)score how well the user's resume/profile matches this application's
    job posting. Synchronous — same pattern as /api/jd/format."""
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    record  = _get_or_404(user_id, app_id)

    from apply import WorkflowConfig

    config = WorkflowConfig(progress=lambda _: None, user_label=actor)

    jd_text = _resolve_jd_text(record, config)
    if not jd_text:
        raise HTTPException(
            422,
            "No job description available to score against — add a posting URL "
            "or link a job description to this application first.",
        )

    try:
        return _run_match_scoring(user_id, app_id, jd_text, actor, scored_by=actor)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except LookupError:
        raise HTTPException(404, "Application not found")
    except Exception as e:
        raise HTTPException(500, f"Scoring failed: {e}")


@router.post("/{app_id}/extract-jd")
async def extract_application_jd(app_id: str, request: Request):
    """Fetch the application's posting URL and extract the job description via Claude."""
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    record  = _get_or_404(user_id, app_id)
    url = (record.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "Application has no posting URL")
    from apply import extract_job_description_from_url, WorkflowConfig
    config = WorkflowConfig(progress=lambda _: None)
    text = extract_job_description_from_url(url, config)
    if not text:
        raise HTTPException(422, "Could not extract job description from that URL")
    record.setdefault("audit_log", []).append(
        _audit_entry("jd_extracted", actor, {"url": url})
    )
    app_store.save_application(user_id, record)
    user_audit.log(user_id, "jd_extracted", actor, app_id=app_id, url=url)
    return {"job_posting": text}


@router.post("/{app_id}/setup-folder", status_code=202)
async def setup_folder(app_id: str, request: Request, response: Response):
    """Create a Drive folder for this application and capture job_description.md
    from the posting URL in the background. Returns immediately."""
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    record  = _get_or_404(user_id, app_id)
    record.setdefault("audit_log", []).append(
        _audit_entry("setup_folder_started", actor, {"url": record.get("url") or ""})
    )
    app_store.save_application(user_id, record)
    user_audit.log(user_id, "setup_folder_started", actor, app_id=app_id,
                   company=record.get("company"), role_title=record.get("role_title"))
    _start_application_pipeline(
        user_id, app_id,
        record["company"], record["role_title"],
        (record.get("url") or ""), actor,
    )
    if FLY_MACHINE_ID:
        response.set_cookie("fly-force-instance-id", FLY_MACHINE_ID,
                            path="/", samesite="lax", httponly=True, secure=True)
    return {"status": "started"}


class PipelineJD(BaseModel):
    job_posting: str


@router.post("/{app_id}/pipeline/jd", status_code=202)
async def pipeline_with_manual_jd(
    app_id: str, body: PipelineJD, request: Request, response: Response,
):
    """Re-run the setup pipeline with a manually pasted job description —
    used when automatic extraction from the posting URL fails. Saves the
    pasted text as job_description.md and re-scores."""
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    record  = _get_or_404(user_id, app_id)

    text = body.job_posting.strip()
    if not text:
        raise HTTPException(400, "job_posting must not be empty")

    _start_application_pipeline(
        user_id, app_id,
        record["company"], record["role_title"],
        (record.get("url") or ""), actor,
        jd_text=text,
    )
    if FLY_MACHINE_ID:
        response.set_cookie("fly-force-instance-id", FLY_MACHINE_ID,
                            path="/", samesite="lax", httponly=True, secure=True)
    return {"status": "started"}


@router.get("/{app_id}/pipeline/stream")
async def stream_pipeline(app_id: str, request: Request):
    """SSE stream of post-create pipeline progress (Drive folder → JD capture →
    match scoring). 404 once the pipeline has been evicted or never started."""
    user_id = request.state.user["user_id"]
    pipe = _pipelines.get(app_id)
    if not pipe:
        raise HTTPException(404, "No pipeline for this application")
    if pipe["user_id"] != user_id and request.state.user.get("role") != "admin":
        raise HTTPException(403, "Access denied")

    q    = pipe["queue"]
    loop = asyncio.get_running_loop()

    async def generate():
        while True:
            try:
                msg = await loop.run_in_executor(None, lambda: q.get(timeout=30))
            except Empty:
                yield ": keepalive\n\n"
                continue
            if msg is None:
                break
            yield f"data: {json.dumps(msg)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/{app_id}/comments/{comment_id}", status_code=204)
async def delete_comment(app_id: str, comment_id: str, request: Request):
    user_id = request.state.user["user_id"]
    actor   = _actor(request)
    record  = _get_or_404(user_id, app_id)

    original_len = len(record.get("comments", []))
    deleted = [c for c in record.get("comments", []) if c["id"] == comment_id]
    record["comments"] = [c for c in record.get("comments", []) if c["id"] != comment_id]

    if len(record["comments"]) == original_len:
        raise HTTPException(404, "Comment not found")

    if deleted:
        record.setdefault("audit_log", []).append(
            _audit_entry("comment_deleted", actor, {"comment_id": comment_id})
        )
    record["updated_at"] = _now()
    record["updated_by"] = actor
    app_store.save_application(user_id, record)
    if deleted:
        user_audit.log(user_id, "comment_deleted", actor, app_id=app_id,
                       comment_id=comment_id)
