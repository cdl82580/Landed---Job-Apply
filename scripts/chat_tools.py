"""
scripts/chat_tools.py — read-only tools the support chatbot can call to look
up the logged-in user's own data.

Isolation: no tool schema has a user_id parameter. run_tool() closes over the
user_id taken from the verified session, so the model can never name another
user. IDs the model does supply (app_id, run_id, folder_id) are always looked
up under that user_id.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from scripts import agent_runs as run_store
from scripts import applications as app_store
from scripts import calendar as cal_store
from scripts import storage

_MAX_LIST_ITEMS = 50
_MAX_TEXT_CHARS = 6000
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

CHAT_TOOLS = [
    {
        "name": "get_my_profile",
        "description": "The logged-in user's profile text and structured profile fields, and whether a master resume is on file.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_my_applications",
        "description": "The user's job applications (company, role, status, priority, tags, dates, match score). Optionally filter by status.",
        "input_schema": {
            "type": "object",
            "properties": {"status": {"type": "string", "description": "e.g. Applied, Interviewing, Offer"}},
        },
    },
    {
        "name": "get_my_application",
        "description": "Full detail for one of the user's applications, including notes, comments and linked runs.",
        "input_schema": {
            "type": "object",
            "properties": {"app_id": {"type": "string"}},
            "required": ["app_id"],
        },
    },
    {
        "name": "list_my_agent_runs",
        "description": "The user's recent agent runs (resume/cover letter generation, interview prep, etc.) with status and company/role.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_my_agent_run",
        "description": "Detail for one of the user's agent runs.",
        "input_schema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
    },
    {
        "name": "list_my_calendar_events",
        "description": "The user's calendar events (interviews, follow-ups). With no bounds this returns only "
            "events from now onward (upcoming). To include past events pass 'from' explicitly, e.g. 1970-01-01T00:00:00Z.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from": {"type": "string", "description": "ISO datetime lower bound (default: now)"},
                "to": {"type": "string", "description": "ISO datetime upper bound"},
            },
        },
    },
    {
        "name": "list_my_drive_folders",
        "description": "The user's Google Drive run folders (output of past agent runs).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_my_drive_job_posting",
        "description": "The saved job posting text from one of the user's Drive run folders.",
        "input_schema": {
            "type": "object",
            "properties": {"folder_id": {"type": "string"}},
            "required": ["folder_id"],
        },
    },
]

_APP_DETAIL_DROP = {"audit_log"}

# Write tools never run on the model's say-so alone: chainlit_app.py shows the
# user describe_write() and only calls run_tool() after they click Confirm.
# They reuse the router handlers so validation, audit logs and notifications
# are identical to the web UI.
_STATUSES = [
    "Not Applying", "Researching", "Applied", "Phone Screen",
    "Interviewing", "On Hold", "Offer", "Rejected", "No Response",
]
_EVENT_TYPES = ["interview", "phone_screen", "follow_up", "deadline", "offer_deadline", "prep", "custom"]

WRITE_TOOLS = [
    {
        "name": "update_application_status",
        "description": "Change the status of one of the user's applications. The user is asked to confirm before it runs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "app_id": {"type": "string"},
                "status": {"type": "string", "enum": _STATUSES},
            },
            "required": ["app_id", "status"],
        },
    },
    {
        "name": "add_application_comment",
        "description": "Add a comment to one of the user's applications. The user is asked to confirm before it runs.",
        "input_schema": {
            "type": "object",
            "properties": {"app_id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["app_id", "text"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": (
            "Create a calendar event for the user. 'datetime' is ISO 8601 UTC (e.g. 2026-06-10T14:00:00Z); "
            "convert from the user's local time and ask for their timezone if unclear. "
            "The user is asked to confirm before it runs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "datetime": {"type": "string"},
                "event_type": {"type": "string", "enum": _EVENT_TYPES},
                "timezone": {"type": "string", "description": "IANA name for display, e.g. America/New_York"},
                "duration_minutes": {"type": "integer"},
                "notes": {"type": "string"},
                "app_id": {"type": "string", "description": "Link to one of the user's applications"},
            },
            "required": ["title", "datetime"],
        },
    },
    {
        "name": "update_calendar_event",
        "description": "Edit fields on one of the user's existing calendar events (get event_id from list_my_calendar_events). The user is asked to confirm before it runs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "title": {"type": "string"},
                "datetime": {"type": "string"},
                "event_type": {"type": "string", "enum": _EVENT_TYPES},
                "timezone": {"type": "string"},
                "duration_minutes": {"type": "integer"},
                "notes": {"type": "string"},
            },
            "required": ["event_id"],
        },
    },
]

WRITE_TOOL_NAMES = {t["name"] for t in WRITE_TOOLS}

_EVENT_FIELDS = ("title", "datetime", "event_type", "timezone", "duration_minutes", "notes")


def describe_write(name: str, args: dict, user_id: str) -> str:
    """Plain-language summary of a pending write, shown to the user to confirm."""
    args = args or {}
    if name in ("update_application_status", "add_application_comment"):
        rec = app_store.get_application(user_id, args.get("app_id", "")) if _valid_id(args.get("app_id")) else None
        label = f"{rec.get('company')} — {rec.get('role_title')}" if rec else "an application"
        if name == "update_application_status":
            return f"Change status of **{label}** from {rec.get('status') if rec else '?'} to **{args.get('status')}**."
        return f"Add a comment to **{label}**:\n\n> {str(args.get('text', ''))[:500]}"
    if name == "create_calendar_event":
        return (
            f"Create event **{args.get('title')}** ({args.get('event_type', 'custom')}) "
            f"at {args.get('datetime')} {args.get('timezone', '')}".rstrip()
            + (f", {args['duration_minutes']} min" if args.get("duration_minutes") else "") + "."
        )
    if name == "update_calendar_event":
        changes = ", ".join(f"{k} → {args[k]}" for k in _EVENT_FIELDS if k in args)
        return f"Update calendar event: {changes or 'no changes'}."
    return f"Run {name}."


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_TEXT_CHARS:
        return value[:_MAX_TEXT_CHARS] + "…[truncated]"
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v) for v in value[:_MAX_LIST_ITEMS]]
    return value


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def _user_drive_folders(user_id: str, email: str) -> list[dict]:
    from apply import WorkflowConfig, list_gdrive_run_folders  # noqa: PLC0415
    config = WorkflowConfig(progress=lambda _: None, user_label=email)
    return list_gdrive_run_folders(email, config) or []


def _linked_folder_ids(user_id: str) -> set[str]:
    items = app_store.list_applications(user_id).get("items") or []
    return {
        run.get("gdrive_folder_id")
        for a in items
        for run in (a.get("linked_runs") or [])
        if run.get("gdrive_folder_id")
    }


def _fake_request(user_id: str, email: str):
    from types import SimpleNamespace  # noqa: PLC0415
    return SimpleNamespace(state=SimpleNamespace(user={"user_id": user_id, "email": email}))


def _write_dispatch(name: str, args: dict, user_id: str, email: str) -> Any:
    import asyncio  # noqa: PLC0415
    from fastapi import HTTPException, Response  # noqa: PLC0415
    from pydantic import ValidationError  # noqa: PLC0415
    from routers import applications as apps_router  # noqa: PLC0415
    from routers import calendar as cal_router  # noqa: PLC0415

    req = _fake_request(user_id, email)
    try:
        if name in ("update_application_status", "add_application_comment"):
            if not _valid_id(args.get("app_id")):
                return {"error": "invalid app_id"}
            if name == "update_application_status":
                body = apps_router.ApplicationUpdate(status=args.get("status"))
                rec = asyncio.run(apps_router.update_application(args["app_id"], body, req, Response()))
                return {"ok": True, "id": rec["id"], "status": rec["status"]}
            body = apps_router.CommentCreate(text=str(args.get("text", "")))
            c = asyncio.run(apps_router.add_comment(args["app_id"], body, req))
            return {"ok": True, "comment_id": c["id"]}

        if name == "create_calendar_event":
            fields = {k: args[k] for k in (*_EVENT_FIELDS, "app_id") if k in args}
            ev = asyncio.run(cal_router.create_calendar_event(cal_router.EventCreate(**fields), req))
            return {"ok": True, "event_id": ev["id"], "title": ev["title"], "datetime": ev["datetime"]}

        if name == "update_calendar_event":
            fields = {k: args[k] for k in _EVENT_FIELDS if k in args}
            body = cal_router.EventUpdate(**fields)
            ev = asyncio.run(cal_router.update_calendar_event(str(args.get("event_id", "")), body, req))
            return {"ok": True, "event_id": ev["id"], "title": ev["title"], "datetime": ev["datetime"]}
    except HTTPException as exc:
        return {"error": str(exc.detail)}
    except ValidationError as exc:
        return {"error": "; ".join(e["msg"] for e in exc.errors())}
    return {"error": f"unknown tool {name}"}


def _dispatch(name: str, args: dict, user_id: str, email: str) -> Any:
    if name in WRITE_TOOL_NAMES:
        return _write_dispatch(name, args, user_id, email)

    if name == "get_my_profile":
        return {
            "profile_text": storage.get_profile(user_id) or "",
            "profile_fields": storage.get_profile_fields(user_id),
            "has_resume": storage.has_resume(user_id),
        }

    if name == "list_my_applications":
        status = args.get("status") if isinstance(args.get("status"), str) else None
        items = app_store.list_applications(user_id, status=status).get("items") or []
        return {"total": len(items), "applications": items[:_MAX_LIST_ITEMS]}

    if name == "get_my_application":
        if not _valid_id(args.get("app_id")):
            return {"error": "invalid app_id"}
        record = app_store.get_application(user_id, args["app_id"])
        if not record:
            return {"error": "not found"}
        return {k: v for k, v in record.items() if k not in _APP_DETAIL_DROP}

    if name == "list_my_agent_runs":
        runs = run_store.list_for_user(user_id)
        return {"total": len(runs), "runs": runs[:_MAX_LIST_ITEMS]}

    if name == "get_my_agent_run":
        if not _valid_id(args.get("run_id")):
            return {"error": "invalid run_id"}
        record = run_store.get(user_id, args["run_id"])
        return record or {"error": "not found"}

    if name == "list_my_calendar_events":
        from_dt = args.get("from") if isinstance(args.get("from"), str) else None
        to_dt = args.get("to") if isinstance(args.get("to"), str) else None
        if not from_dt:
            from_dt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        events = cal_store.list_events(user_id, from_dt=from_dt, to_dt=to_dt)
        return {"total": len(events), "events": events[:_MAX_LIST_ITEMS]}

    if name == "list_my_drive_folders":
        return {"folders": _user_drive_folders(user_id, email)[:_MAX_LIST_ITEMS]}

    if name == "get_my_drive_job_posting":
        folder_id = args.get("folder_id")
        if not _valid_id(folder_id):
            return {"error": "invalid folder_id"}
        allowed = _linked_folder_ids(user_id) | {
            f.get("id") for f in _user_drive_folders(user_id, email)
        }
        if folder_id not in allowed:
            return {"error": "not found"}
        from apply import WorkflowConfig, get_gdrive_job_posting  # noqa: PLC0415
        config = WorkflowConfig(progress=lambda _: None, user_label=email)
        text = get_gdrive_job_posting(folder_id, config)
        return {"job_posting": text} if text else {"error": "not found"}

    return {"error": f"unknown tool {name}"}


def run_tool(name: str, args: dict | None, user_id: str, email: str) -> str:
    """Execute a chat tool for `user_id` and return a JSON string for the model."""
    if not user_id or user_id == "anonymous":
        return json.dumps({"error": "not authenticated"})
    try:
        result = _dispatch(name, args or {}, user_id, email)
    except Exception as exc:  # tool failure must not break the chat turn
        return json.dumps({"error": f"lookup failed: {type(exc).__name__}"})
    return json.dumps(_clip(result), default=str)
