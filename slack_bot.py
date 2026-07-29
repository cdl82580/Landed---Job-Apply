"""
slack_bot.py — Slack bot for the Job Application Agent.

Environment variables required:
  SLACK_BOT_TOKEN       xoxb-... token from the Slack app
  SLACK_SIGNING_SECRET  signing secret from the Slack app Basic Information page
  BOT_API_KEY           must match the BOT_API_KEY set on the Fly.io app
  JOB_APPLY_API_URL     base URL of the deployed app (default: https://apply.cdlav.us)

Run locally:
  python slack_bot.py

The bot listens on port 3000 (configurable via PORT env var).
In production, run behind a reverse proxy or expose directly via Fly.io.

Environment variables (optional):
  ANTHROPIC_API_KEY     — enables model auto-upgrade checks (every 6 h)
  SLACK_NOTIFY_CHANNEL  — channel/DM user ID for model upgrade notifications
  SLACK_NOTIFY_USER_ID  — Slack user ID for calendar reminder DMs

Slash commands handled:
  /apply           — generate resume + cover letter for a job
  /aq              — answer an application question using resume & JD
  /prep            — generate interview prep doc
  /thankyou        — generate a post-interview thank-you email
  /optimize        — refine an existing run's documents from a prompt
  /rescore         — re-score resume/JD match for an application
  /runs            — list recent Drive run folders

  /cal-today       — show today's calendar events
  /cal-week        — show events in the next 7 days
  /cal-add         — add a calendar event with reminders (modal)
  /cal-view        — view full event details (modal)
  /cal-delete      — delete a calendar event (modal + confirm)

  /tracker         — pipeline summary (counts by status)
  /track-list      — list active applications
  /track-view      — view full application details (modal)
  /track-add       — add a new application record (modal)
  /track-update    — update an application's status (modal)
  /track-note      — add a comment to an application (modal)
  /track-delete    — delete an application (modal + confirm)

  /company         — search company info via Logo.dev
  /whoami          — show account details

  /profile-resume  — upload a new master resume (.docx via DM file upload)
  /profile-guide   — edit profile & voice guide (modal)
  /notifications   — view and toggle email notification preferences

  /help            — full command reference
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

try:
    import anthropic as _anthropic_sdk
    _HAS_ANTHROPIC = True
except ImportError:
    _HAS_ANTHROPIC = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SLACK_BOT_TOKEN      = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
SLACK_APP_TOKEN      = os.environ.get("SLACK_APP_TOKEN", "")  # xapp-... for Socket Mode
BOT_API_KEY          = os.environ["BOT_API_KEY"]
API_BASE             = os.environ.get("JOB_APPLY_API_URL", "https://apply.cdlav.us").rstrip("/")
PORT                 = int(os.environ.get("PORT", "3000"))

# Defense in depth for handle_message_with_file below: url_private(_download)
# is normally a files.slack.com URL from a real Slack `message` event, but
# nothing else validates that before we send SLACK_BOT_TOKEN to it as a
# Bearer header — cap it to Slack's own file-hosting domain.
_TRUSTED_SLACK_FILE_HOST_SUFFIX = ".slack.com"


def _is_trusted_slack_file_host(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host.endswith(_TRUSTED_SLACK_FILE_HOST_SUFFIX)

# Slack user ID authorised to run test suites (resolved once at startup).
# Set TEST_RUNNER_SLACK_USER_ID as a Fly secret, or falls back to
# SLACK_NOTIFY_USER_ID. Leave unset to disable /run-tests entirely.
TEST_RUNNER_SLACK_USER_ID = os.environ.get(
    "TEST_RUNNER_SLACK_USER_ID",
    os.environ.get("SLACK_NOTIFY_USER_ID", ""),
)

ROUND_TYPES = ["recruiter_screen", "hiring_manager", "technical", "panel", "final", "take_home"]

VALID_STATUSES  = ["Not Applying", "Researching", "Applied", "Phone Screen",
                   "Interviewing", "On Hold", "Offer", "Rejected"]
VALID_PRIORITIES = ["High", "Medium", "Low"]

STATUS_EMOJI = {
    "Interviewing":  "🎯",
    "Phone Screen":  "📞",
    "Applied":       "✅",
    "Researching":   "🔬",
    "On Hold":       "⏸️",
    "Offer":         "🎉",
    "Rejected":      "❌",
    "Not Applying":  "🚫",
}

TRACKER_URL = f"{API_BASE}/tracking.html"

# Publishable Logo.dev CDN key — same one frontend/*.html hardcode client-side
# and teams_bot/bot.py uses for its response-card company logos.
LOGODEV_PUBLIC_KEY = os.environ.get("LOGODEV_PUBLIC_KEY", "pk_U3oIYbhyTvinmftvOvCTJg")


def _logo_url(domain: str, size: int = 24) -> str:
    if not domain:
        return ""
    return f"https://img.logo.dev/{domain}?token={LOGODEV_PUBLIC_KEY}&size={size}&format=webp&retina=true"


def _logo_element(domain: str, alt_text: str = "", size: int = 24) -> dict | None:
    """Slack "image" element sized for a context block, where Slack renders
    it small and inline next to text — a section block's accessory image
    renders as a much bigger right-aligned thumbnail instead, which is why
    this isn't used there. None if there's no domain on file."""
    url = _logo_url(domain, size=size)
    if not url:
        return None
    return {"type": "image", "image_url": url, "alt_text": alt_text or "logo"}


def _completion_blocks(title: str, company: str, role: str, domain: str, detail: str) -> list[dict]:
    """Header + logo/company-role row + detail text — the Slack analogue of
    the Teams Adaptive Card completion cards (bot.py's _submit_apply etc.):
    a bold title, the company (with logo when known) and role right below
    it, then whatever detail text the specific command wants to show."""
    subtitle_elements = []
    logo_el = _logo_element(domain, alt_text=company)
    if logo_el:
        subtitle_elements.append(logo_el)
    subtitle_elements.append({"type": "mrkdwn", "text": f"{company} — {role}"})
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}},
        {"type": "context", "elements": subtitle_elements},
        {"type": "section", "text": {"type": "mrkdwn", "text": detail}},
    ]


def _fields_blocks(title: str, pairs: list[tuple[str, str]]) -> list[dict]:
    """Header + a Block Kit 'fields' 2-column grid — the Slack analogue of
    an Adaptive Card FactSet (used by /tracker, /whoami)."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}},
        {
            "type": "section",
            "fields": [{"type": "mrkdwn", "text": f"*{label}*\n{value}"} for label, value in pairs],
        },
    ]


app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)

# ---------------------------------------------------------------------------
# Access control — this bot operates a single Job Apply account (Corey's) via
# a shared BOT_API_KEY with no further per-user check downstream. Without
# this gate, any Slack workspace member/guest who can DM or slash-command the
# bot would have full control of that account: delete tracked applications,
# overwrite the master resume, or trigger paid Claude/Drive runs. Restrict
# every listener to the one allowed user, the same way /run-tests already
# restricts itself. Left unset, this fails open (matches SLACK_NOTIFY_USER_ID's
# existing "unset = feature off for this deployment" convention) — set it as
# a Fly secret on any workspace with more than one member.
# ---------------------------------------------------------------------------
_ALLOWED_SLACK_USER_ID = os.environ.get("SLACK_NOTIFY_USER_ID", "")


def _extract_slack_user_id(body: dict) -> str:
    """Best-effort caller Slack user ID across the different payload shapes
    Bolt hands middleware: slash commands (user_id), block actions / view
    submissions (user.id), and events (event.user)."""
    if not isinstance(body, dict):
        return ""
    if body.get("user_id"):
        return str(body["user_id"])
    user = body.get("user")
    if isinstance(user, dict) and user.get("id"):
        return str(user["id"])
    if isinstance(user, str) and user:
        return user
    event = body.get("event")
    if isinstance(event, dict) and event.get("user"):
        return str(event["user"])
    return ""


@app.middleware
def _restrict_to_owner(body: dict, next):
    if not _ALLOWED_SLACK_USER_ID:
        next()
        return
    caller = _extract_slack_user_id(body)
    if caller and caller != _ALLOWED_SLACK_USER_ID:
        print(f"[slack_bot] ignored interaction from unauthorized user {caller}")
        return  # swallow — no next(), no response sent
    next()


_log = logging.getLogger(__name__)


def _error_text(action: str, exc: Exception | str) -> str:
    """Generic chat-facing error message for a failed action — the real
    exception is logged server-side instead of shown to the user. Bot error
    messages that interpolate str(exc) directly can otherwise leak internal
    detail (request URLs, HTTP response bodies) to anyone who can reach the
    bot. Now that the bot is gated to a single owner (see
    _restrict_to_owner above) the audience is smaller, but there's no reason
    to keep showing raw internals to that one user either."""
    _log.warning("%s: %s", action, exc, exc_info=True)
    return f":x: {action}. Please try again — if it keeps happening, check the logs."


# ---------------------------------------------------------------------------
# API helpers — agent runs
# ---------------------------------------------------------------------------

def _api(method: str, path: str, **kwargs) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {BOT_API_KEY}"
    return getattr(requests, method)(f"{API_BASE}{path}", headers=headers, timeout=30, **kwargs)


def _post_run(job_posting: str, company: str, role: str, contact: str = "", domain: str = "") -> dict:
    r = _api("post", "/api/run", json={
        "job_posting": job_posting,
        "company": company,
        "role": role,
        "contact": contact or None,
        "domain": domain or None,
    })
    r.raise_for_status()
    return r.json()


def _post_prep(job_posting: str, company: str, role: str,
               round_type: str, focus: str = "", interviewer: str = "",
               interview_date: str = "", interview_time: str = "", location: str = "",
               domain: str = "") -> dict:
    r = _api("post", "/api/prep", json={
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


def _poll_run(run_id: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api("get", f"/api/run/{run_id}/status")
        r.raise_for_status()
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(5)
    return {"status": "timeout", "error": "Timed out waiting for run to complete"}


def _poll_prep(prep_id: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api("get", f"/api/prep/{prep_id}/status")
        r.raise_for_status()
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(5)
    return {"status": "timeout", "error": "Timed out waiting for prep to complete"}


def _post_aq(question: str, job_posting: str, company: str, role: str,
             tone: str = "professional", char_limit: int | None = None) -> dict:
    payload: dict = {
        "question": question,
        "job_posting": job_posting,
        "company": company,
        "role": role,
        "tone": tone,
    }
    if char_limit:
        payload["char_limit"] = char_limit
    r = _api("post", "/api/aq", json=payload)
    r.raise_for_status()
    return r.json()


def _poll_aq(aq_id: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api("get", f"/api/aq/{aq_id}/status")
        r.raise_for_status()
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(5)
    return {"status": "timeout", "error": "Timed out waiting for answer to complete"}


def _submit_aq_clarifications(aq_id: str, answers: dict[str, str]) -> dict:
    r = _api("post", f"/api/aq/{aq_id}/clarify", json={"answers": answers})
    r.raise_for_status()
    return r.json()


def _post_thankyou(job_posting: str, company: str, role: str, round_type: str,
                   interviewer: str = "", topics: str = "",
                   tone: str = "professional") -> dict:
    r = _api("post", "/api/thankyou", json={
        "job_posting": job_posting,
        "company": company,
        "role": role,
        "round_type": round_type,
        "interviewer": interviewer or None,
        "topics": topics or None,
        "tone": tone,
    })
    r.raise_for_status()
    return r.json()


def _poll_thankyou(ty_id: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api("get", f"/api/thankyou/{ty_id}/status")
        r.raise_for_status()
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(5)
    return {"status": "timeout", "error": "Timed out waiting for thank-you email to complete"}


# ---------------------------------------------------------------------------
# API helpers — application tracker
# ---------------------------------------------------------------------------

def _get_apps(status: str | None = None) -> list[dict]:
    """Fetch all application index entries, optionally filtered by status."""
    params = {}
    if status:
        params["status"] = status
    r = _api("get", "/api/applications", params=params)
    r.raise_for_status()
    data = r.json()
    return data.get("items", data) if isinstance(data, dict) else data


def _get_app(app_id: str) -> dict:
    r = _api("get", f"/api/applications/{app_id}")
    r.raise_for_status()
    return r.json()


def _get_saved_job_posting(record: dict) -> str | None:
    """Saved job posting text from an application's most recently linked
    Drive folder, or None if there isn't one yet — same lookup teams_bot/
    bot.py:_resolve_app_and_jd does. Backs the apply/prep/aq JD-paste fallback."""
    runs = [r for r in (record.get("linked_runs") or []) if r.get("gdrive_folder_id")]
    if not runs:
        return None
    runs.sort(key=lambda r: r.get("linked_at", ""), reverse=True)
    folder_id = runs[0]["gdrive_folder_id"]
    try:
        r = _api("get", f"/api/gdrive/runs/{folder_id}/job_posting")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("job_posting")
    except Exception:
        return None


def _jd_paste_view(callback_id: str, private_metadata: str) -> dict:
    """Follow-up modal asking for the JD text when none is saved yet, carrying
    forward everything already collected in the first step via private_metadata
    (Slack's per-modal state-passing string, read back in the *_final handler)."""
    return {
        "type": "modal",
        "callback_id": callback_id,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "Paste Job Description"},
        "submit": {"type": "plain_text", "text": "Generate"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "I couldn't find a saved job description for this application yet — paste it below.",
                },
            },
            {
                "type": "input",
                "block_id": "job_posting",
                "label": {"type": "plain_text", "text": "Job posting"},
                "element": {"type": "plain_text_input", "action_id": "value", "multiline": True,
                            "placeholder": {"type": "plain_text", "text": "Paste the full job description here…"}},
            },
        ],
    }


def _create_app(payload: dict) -> dict:
    r = _api("post", "/api/applications", json=payload)
    r.raise_for_status()
    return r.json()


def _update_app(app_id: str, payload: dict) -> dict:
    r = _api("put", f"/api/applications/{app_id}", json=payload)
    r.raise_for_status()
    return r.json()


def _delete_app(app_id: str) -> None:
    r = _api("delete", f"/api/applications/{app_id}")
    r.raise_for_status()


def _add_comment(app_id: str, text: str) -> dict:
    r = _api("post", f"/api/applications/{app_id}/comments", json={"text": text})
    r.raise_for_status()
    return r.json()


# Statuses excluded from the default Slack select — typically low-value for
# update/note/delete actions and too numerous to show by default.
_INACTIVE_STATUSES = {"Not Applying", "Rejected"}
# Slack static_select hard cap
_SLACK_MAX_OPTIONS = 100


def _app_options(apps: list[dict] | None = None, active_only: bool = True) -> list[dict]:
    """Return Slack static_select options (max 100).

    active_only=True (default) excludes 'Not Applying' and 'Rejected' to keep
    the list short and relevant. Pass active_only=False for the delete command
    where you may want to clean up any record.
    """
    if apps is None:
        apps = _get_apps()

    if active_only:
        apps = [a for a in apps if a.get("status") not in _INACTIVE_STATUSES]

    order = {s: i for i, s in enumerate(VALID_STATUSES)}
    apps  = sorted(apps, key=lambda a: (order.get(a.get("status", ""), 99), a.get("company", "")))

    options = []
    for a in apps[:_SLACK_MAX_OPTIONS]:
        label = f"{a.get('company', '?')} · {a.get('role_title', '?')} ({a.get('status', '?')})"
        if len(label) > 75:
            label = label[:72] + "…"
        options.append({
            "text":  {"type": "plain_text", "text": label},
            "value": a["id"],
        })
    return options


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return "—"
    parts = iso.split("T")[0].split("-")
    return f"{int(parts[1])}/{int(parts[2])}/{parts[0][2:]}" if len(parts) == 3 else iso


def _app_line(a: dict) -> str:
    emoji  = STATUS_EMOJI.get(a.get("status", ""), "•")
    name   = f"*{a.get('company', '?')}* · {a.get('role_title', '?')}"
    status = a.get("status", "?")
    date   = _fmt_date(a.get("date_applied"))
    url    = a.get("url", "")
    parts  = [f"{emoji} {name}", f"_{status}_"]
    if date != "—":
        parts.append(f"Applied {date}")
    if url:
        parts.append(f"<{url}|Job Post ↗>")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# /tracker — pipeline summary
# ---------------------------------------------------------------------------

@app.command("/tracker")
def tracker_command(ack, respond):
    ack()
    try:
        apps = _get_apps()
    except Exception as exc:
        respond(_error_text("Could not reach the tracker", exc))
        return

    counts: dict[str, int] = {s: 0 for s in VALID_STATUSES}
    for a in apps:
        s = a.get("status", "")
        if s in counts:
            counts[s] += 1

    pairs = [
        (f"{STATUS_EMOJI[status]} {status}", str(n))
        for status in VALID_STATUSES
        if (n := counts[status])
    ]
    header = f":bar_chart: Application Pipeline ({len(apps)} total)"
    blocks = _fields_blocks(header, pairs)
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"<{TRACKER_URL}|Open Tracker →>"}],
    })
    respond(blocks=blocks, text=header)


# ---------------------------------------------------------------------------
# /track-list [status] — list applications
# ---------------------------------------------------------------------------

@app.command("/track-list")
def track_list_command(ack, respond, body):
    ack()
    raw_status = body.get("text", "").strip()

    # Normalise: "interviewing" → "Interviewing", "phone screen" → "Phone Screen"
    status_filter: str | None = None
    if raw_status:
        matches = [s for s in VALID_STATUSES if s.lower() == raw_status.lower()]
        if matches:
            status_filter = matches[0]
        else:
            respond(
                f":x: Unknown status `{raw_status}`. Valid values: "
                + ", ".join(f"`{s}`" for s in VALID_STATUSES)
            )
            return

    try:
        apps = _get_apps(status=status_filter)
    except Exception as exc:
        respond(_error_text("Could not reach the tracker", exc))
        return

    if not apps:
        label = f"*{status_filter}*" if status_filter else "active"
        respond(f"No {label} applications found.")
        return

    # Sort: most active first
    order = {s: i for i, s in enumerate(VALID_STATUSES)}
    apps = sorted(apps, key=lambda a: (order.get(a.get("status", ""), 99), a.get("company", "")))

    header = f":clipboard: *Applications{' — ' + status_filter if status_filter else ''}* ({len(apps)} total)"
    shown  = apps[:15]

    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}]

    for a in shown:
        elements = []
        logo_el = _logo_element(a.get("domain", ""), alt_text=a.get("company", ""))
        if logo_el:
            elements.append(logo_el)
        elements.append({"type": "mrkdwn", "text": _app_line(a)})
        blocks.append({"type": "context", "elements": elements})

    if len(apps) > 15:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": f"_…and {len(apps) - 15} more. <{TRACKER_URL}|View all in tracker →>_"}],
        })
    else:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"<{TRACKER_URL}|Open Tracker →>"}],
        })

    respond(blocks=blocks, text=header)


# ---------------------------------------------------------------------------
# /track-add — add a new application
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Logo.dev external_select handler (used by /track-add)
# ---------------------------------------------------------------------------

@app.options("company_search")
def handle_company_search(ack, payload):
    """Return Logo.dev results for the company external_select."""
    query = (payload.get("value") or "").strip()
    if len(query) < 2:
        ack(options=[])
        return
    try:
        r = _api("get", "/api/companies/search", params={"q": query})
        r.raise_for_status()
        results = r.json()
        options = []
        for c in results[:5]:
            name   = c.get("name", "?")
            domain = c.get("domain", "")
            label  = f"{name}  ({domain})" if domain else name
            value  = f"{name}|||{domain}"
            options.append({
                "text":  {"type": "plain_text", "text": label[:75]},
                "value": value[:75],
            })
        ack(options=options)
    except Exception:
        ack(options=[])


def _track_add_blocks(prefill: dict | None = None) -> list:
    """Return blocks for the Add Application modal. prefill not used for add."""
    def _sel_opt(val):
        return {"text": {"type": "plain_text", "text": val}, "value": val}

    return [
        # ── Company ──────────────────────────────────────────────────
        {
            "type": "input",
            "block_id": "company_search",
            "label": {"type": "plain_text", "text": "Company"},
            "hint":  {"type": "plain_text", "text": "Start typing to search — name and domain will auto-fill."},
            "element": {
                "type": "external_select",
                "action_id": "company_search",
                "placeholder": {"type": "plain_text", "text": "Search company name…"},
                "min_query_length": 2,
            },
        },
        {
            "type": "input",
            "block_id": "domain",
            "optional": True,
            "label": {"type": "plain_text", "text": "Domain (optional — auto-filled if found above)"},
            "element": {"type": "plain_text_input", "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "e.g. salesforce.com"}},
        },
        # ── Role ─────────────────────────────────────────────────────
        {
            "type": "input",
            "block_id": "role_title",
            "label": {"type": "plain_text", "text": "Role Title"},
            "element": {"type": "plain_text_input", "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "Solutions Engineer"}},
        },
        # ── Status / Priority ────────────────────────────────────────
        {
            "type": "input",
            "block_id": "status",
            "label": {"type": "plain_text", "text": "Status"},
            "element": {
                "type": "static_select", "action_id": "value",
                "initial_option": _sel_opt("Researching"),
                "options": [_sel_opt(s) for s in VALID_STATUSES],
            },
        },
        # ── Details ──────────────────────────────────────────────────
        {
            "type": "input",
            "block_id": "date_applied",
            "optional": True,
            "label": {"type": "plain_text", "text": "Date Applied"},
            "element": {"type": "datepicker", "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "Select date"}},
        },
        {
            "type": "input",
            "block_id": "job_source",
            "optional": True,
            "label": {"type": "plain_text", "text": "Job Source"},
            "element": {"type": "plain_text_input", "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "LinkedIn, Indeed, Referral…"}},
        },
        {
            "type": "input",
            "block_id": "location",
            "optional": True,
            "label": {"type": "plain_text", "text": "Location / Remote"},
            "element": {"type": "plain_text_input", "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "Remote, Boston, Hybrid…"}},
        },
        {
            "type": "input",
            "block_id": "salary_range",
            "optional": True,
            "label": {"type": "plain_text", "text": "Salary Range"},
            "element": {"type": "plain_text_input", "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "e.g. $130k – $160k"}},
        },
        {
            "type": "input",
            "block_id": "url",
            "optional": True,
            "label": {"type": "plain_text", "text": "Job Posting URL"},
            "element": {"type": "plain_text_input", "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "https://…"}},
        },
        # ── DUA ──────────────────────────────────────────────────────
        {
            "type": "input",
            "block_id": "dua",
            "optional": True,
            "label": {"type": "plain_text", "text": "Unemployment (DUA)"},
            "element": {
                "type": "checkboxes", "action_id": "value",
                "options": [{"text": {"type": "plain_text", "text": "Reported this application to DUA"},
                             "value": "yes"}],
            },
        },
        # ── Recruiter ────────────────────────────────────────────────
        {
            "type": "input",
            "block_id": "recruiter_name",
            "optional": True,
            "label": {"type": "plain_text", "text": "Recruiter Name"},
            "element": {"type": "plain_text_input", "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "Jane Smith"}},
        },
        {
            "type": "input",
            "block_id": "recruiter_email",
            "optional": True,
            "label": {"type": "plain_text", "text": "Recruiter Email"},
            "element": {"type": "plain_text_input", "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "jane@company.com"}},
        },
        # ── Note ─────────────────────────────────────────────────────
        {
            "type": "input",
            "block_id": "note",
            "optional": True,
            "label": {"type": "plain_text", "text": "Initial Note"},
            "element": {"type": "plain_text_input", "action_id": "value",
                        "multiline": True,
                        "placeholder": {"type": "plain_text", "text": "Any notes about this role…"}},
        },
    ]


@app.command("/track-add")
def track_add_command(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "track_add_submit",
            "title": {"type": "plain_text", "text": "Add Application"},
            "submit": {"type": "plain_text", "text": "Add"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": _track_add_blocks(),
        },
    )


@app.view("track_add_submit")
def track_add_view_submit(ack, body, client, view):
    ack()
    vals    = view["state"]["values"]
    channel = body["user"]["id"]

    # Company — decode "name|||domain" from external_select
    # block_id="company_search", action_id="company_search"
    company_raw = (
        (vals.get("company_search", {}).get("company_search", {}) or {})
        .get("selected_option", {})
        .get("value", "") or ""
    )
    if "|||" in company_raw:
        company, domain = company_raw.split("|||", 1)
    else:
        company = company_raw.strip()
        domain  = ""
    # Domain override / fallback
    domain_override = ((vals.get("domain", {}).get("value", {}) or {}).get("value") or "").strip()
    if domain_override:
        domain = domain_override

    def _txt(block):
        return ((vals.get(block, {}).get("value", {}) or {}).get("value") or "").strip()

    def _sel(block, fallback=""):
        opt = (vals.get(block, {}).get("value", {}) or {}).get("selected_option", {})
        return (opt.get("value") or fallback)

    def _date(block):
        return ((vals.get(block, {}).get("value", {}) or {}).get("selected_date") or None)

    def _dua(block):
        opts = ((vals.get(block, {}).get("value", {}) or {}).get("selected_options") or [])
        return any(o.get("value") == "yes" for o in opts)

    role_title     = _txt("role_title")
    status         = _sel("status", "Researching")
    date_applied   = _date("date_applied")
    job_source     = _txt("job_source")
    location       = _txt("location")
    salary_range   = _txt("salary_range")
    url            = _txt("url")
    dua            = _dua("dua")
    recruiter_name = _txt("recruiter_name")
    recruiter_email= _txt("recruiter_email")
    note           = _txt("note")

    if date_applied:
        date_applied = f"{date_applied}T00:00:00Z"

    try:
        record = _create_app({
            "company":         company,
            "domain":          domain,
            "role_title":      role_title,
            "status":          status,
            "date_applied":    date_applied,
            "job_source":      job_source,
            "location":        location,
            "salary_range":    salary_range,
            "url":             url,
            "dua":             dua,
            "recruiter_name":  recruiter_name,
            "recruiter_email": recruiter_email,
        })
        if note:
            _add_comment(record["id"], note)
        client.chat_postMessage(
            channel=channel,
            text=(
                f":white_check_mark: Added *{role_title}* at *{company}* "
                f"({status})\n"
                f"<{TRACKER_URL}?app={record['id']}|View in Tracker →>"
            ),
        )
    except Exception as exc:
        client.chat_postMessage(channel=channel, text=_error_text("Failed to add application", exc))


# ---------------------------------------------------------------------------
# /track-update — update an application's status
# ---------------------------------------------------------------------------

@app.command("/track-update")
def track_update_command(ack, body, client, respond):
    ack()
    try:
        options = _app_options()
    except Exception as exc:
        respond(_error_text("Could not load applications", exc))
        return
    if not options:
        respond("No applications found. Use `/track-add` to create one.")
        return
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "track_update_select",
            "title": {"type": "plain_text", "text": "Update Application"},
            "submit": {"type": "plain_text", "text": "Continue →"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [{
                "type": "input",
                "block_id": "app_id",
                "label": {"type": "plain_text", "text": "Select application to edit"},
                "element": {
                    "type": "static_select",
                    "action_id": "value",
                    "placeholder": {"type": "plain_text", "text": "Select an application…"},
                    "options": options,
                },
            }],
        },
    )


@app.view("track_update_select")
def track_update_select_submit(ack, body, client, view):
    """Step 1 → push the full edit form pre-filled with current values."""
    app_id = view["state"]["values"]["app_id"]["value"]["selected_option"]["value"]
    channel = body["user"]["id"]

    try:
        a = _get_app(app_id)
    except Exception as exc:
        ack()
        client.chat_postMessage(channel=channel, text=_error_text("Could not load application", exc))
        return

    def _sel_opt(val):
        return {"text": {"type": "plain_text", "text": val}, "value": val}

    def _init_opt(val, options_list):
        if val in options_list:
            return _sel_opt(val)
        return None

    # Build blocks pre-filled from the current record
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f":pencil2: *{a.get('role_title', '?')}* at *{a.get('company', '?')}*"},
        },
        # Status
        {
            "type": "input", "block_id": "status",
            "label": {"type": "plain_text", "text": "Status"},
            "element": {
                "type": "static_select", "action_id": "value",
                **( {"initial_option": _sel_opt(a["status"])} if a.get("status") in VALID_STATUSES else {} ),
                "options": [_sel_opt(s) for s in VALID_STATUSES],
            },
        },
        # Date Applied
        {
            "type": "input", "block_id": "date_applied", "optional": True,
            "label": {"type": "plain_text", "text": "Date Applied"},
            "element": {
                "type": "datepicker", "action_id": "value",
                **( {"initial_date": a["date_applied"][:10]} if a.get("date_applied") else {} ),
                "placeholder": {"type": "plain_text", "text": "Select date"},
            },
        },
        # Job Source
        {
            "type": "input", "block_id": "job_source", "optional": True,
            "label": {"type": "plain_text", "text": "Job Source"},
            "element": {
                "type": "plain_text_input", "action_id": "value",
                **( {"initial_value": a["job_source"]} if a.get("job_source") else {} ),
                "placeholder": {"type": "plain_text", "text": "LinkedIn, Indeed, Referral…"},
            },
        },
        # Location
        {
            "type": "input", "block_id": "location", "optional": True,
            "label": {"type": "plain_text", "text": "Location / Remote"},
            "element": {
                "type": "plain_text_input", "action_id": "value",
                **( {"initial_value": a["location"]} if a.get("location") else {} ),
                "placeholder": {"type": "plain_text", "text": "Remote, Boston, Hybrid…"},
            },
        },
        # Salary
        {
            "type": "input", "block_id": "salary_range", "optional": True,
            "label": {"type": "plain_text", "text": "Salary Range"},
            "element": {
                "type": "plain_text_input", "action_id": "value",
                **( {"initial_value": a["salary_range"]} if a.get("salary_range") else {} ),
                "placeholder": {"type": "plain_text", "text": "e.g. $130k – $160k"},
            },
        },
        # URL
        {
            "type": "input", "block_id": "url", "optional": True,
            "label": {"type": "plain_text", "text": "Job Posting URL"},
            "element": {
                "type": "plain_text_input", "action_id": "value",
                **( {"initial_value": a["url"]} if a.get("url") else {} ),
                "placeholder": {"type": "plain_text", "text": "https://…"},
            },
        },
        # DUA
        {
            "type": "input", "block_id": "dua", "optional": True,
            "label": {"type": "plain_text", "text": "Unemployment (DUA)"},
            "element": {
                "type": "checkboxes", "action_id": "value",
                "options": [{"text": {"type": "plain_text", "text": "Reported to DUA"}, "value": "yes"}],
                **( {"initial_options": [{"text": {"type": "plain_text", "text": "Reported to DUA"}, "value": "yes"}]}
                    if a.get("dua") else {} ),
            },
        },
        # Recruiter Name
        {
            "type": "input", "block_id": "recruiter_name", "optional": True,
            "label": {"type": "plain_text", "text": "Recruiter Name"},
            "element": {
                "type": "plain_text_input", "action_id": "value",
                **( {"initial_value": a["recruiter_name"]} if a.get("recruiter_name") else {} ),
                "placeholder": {"type": "plain_text", "text": "Jane Smith"},
            },
        },
        # Recruiter Email
        {
            "type": "input", "block_id": "recruiter_email", "optional": True,
            "label": {"type": "plain_text", "text": "Recruiter Email"},
            "element": {
                "type": "plain_text_input", "action_id": "value",
                **( {"initial_value": a["recruiter_email"]} if a.get("recruiter_email") else {} ),
                "placeholder": {"type": "plain_text", "text": "jane@company.com"},
            },
        },
        # Note
        {
            "type": "input", "block_id": "note", "optional": True,
            "label": {"type": "plain_text", "text": "Add a Note (optional)"},
            "element": {
                "type": "plain_text_input", "action_id": "value",
                "multiline": True,
                "placeholder": {"type": "plain_text", "text": "e.g. Got a callback from recruiter"},
            },
        },
    ]

    ack(response_action="push", view={
        "type": "modal",
        "callback_id": "track_update_edit",
        "title": {"type": "plain_text", "text": "Edit Application"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close":  {"type": "plain_text", "text": "Cancel"},
        "private_metadata": app_id,
        "blocks": blocks,
    })


@app.view("track_update_edit")
def track_update_edit_submit(ack, body, client, view):
    ack()
    app_id  = view["private_metadata"]
    channel = body["user"]["id"]
    vals    = view["state"]["values"]

    def _txt(block):
        return ((vals.get(block, {}).get("value", {}) or {}).get("value") or "").strip()

    def _sel(block, fallback=""):
        opt = (vals.get(block, {}).get("value", {}) or {}).get("selected_option", {})
        return (opt.get("value") or fallback)

    def _date(block):
        return ((vals.get(block, {}).get("value", {}) or {}).get("selected_date") or None)

    def _dua(block):
        opts = ((vals.get(block, {}).get("value", {}) or {}).get("selected_options") or [])
        return any(o.get("value") == "yes" for o in opts)

    date_applied = _date("date_applied")
    if date_applied:
        date_applied = f"{date_applied}T00:00:00Z"

    updates = {
        "status":          _sel("status"),
        "date_applied":    date_applied,
        "job_source":      _txt("job_source") or None,
        "location":        _txt("location") or None,
        "salary_range":    _txt("salary_range") or None,
        "url":             _txt("url") or None,
        "dua":             _dua("dua"),
        "recruiter_name":  _txt("recruiter_name") or None,
        "recruiter_email": _txt("recruiter_email") or None,
    }
    # Strip None values so we don't overwrite fields with null
    updates = {k: v for k, v in updates.items() if v is not None or k in ("dua",)}
    note = _txt("note")

    try:
        record = _update_app(app_id, updates)
        if note:
            _add_comment(app_id, note)
        client.chat_postMessage(
            channel=channel,
            text=(
                f":pencil2: Updated *{record.get('role_title')}* at *{record.get('company')}* "
                f"→ *{updates.get('status', record.get('status'))}*"
                + (f"\n> {note}" if note else "")
                + f"\n<{TRACKER_URL}?app={app_id}|View in Tracker →>"
            ),
        )
    except Exception as exc:
        client.chat_postMessage(channel=channel, text=_error_text("Failed to update", exc))


# ---------------------------------------------------------------------------
# /track-note — add a comment to an application
# ---------------------------------------------------------------------------

@app.command("/track-note")
def track_note_command(ack, body, client, respond):
    ack()
    try:
        options = _app_options()
    except Exception as exc:
        respond(_error_text("Could not load applications", exc))
        return

    if not options:
        respond("No applications found. Use `/track-add` to create one.")
        return

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "track_note_submit",
            "title": {"type": "plain_text", "text": "Add Note"},
            "submit": {"type": "plain_text", "text": "Add Note"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "app_id",
                    "label": {"type": "plain_text", "text": "Application"},
                    "element": {
                        "type": "static_select",
                        "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "Select an application…"},
                        "options": options,
                    },
                },
                {
                    "type": "input",
                    "block_id": "note",
                    "label": {"type": "plain_text", "text": "Note"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "multiline": True,
                                "placeholder": {"type": "plain_text",
                                                "text": "e.g. Spoke with recruiter — next step is HM interview"}},
                },
            ],
        },
    )


@app.view("track_note_submit")
def track_note_view_submit(ack, body, client, view):
    ack()
    vals    = view["state"]["values"]
    channel = body["user"]["id"]

    app_id = vals["app_id"]["value"]["selected_option"]["value"]
    note   = vals["note"]["value"]["value"].strip()

    try:
        rec = _get_app(app_id)
        _add_comment(app_id, note)
        client.chat_postMessage(
            channel=channel,
            text=(
                f":speech_balloon: Note added to *{rec.get('role_title')}* at *{rec.get('company')}*\n"
                f"> {note}\n"
                f"<{TRACKER_URL}?app={app_id}|View in Tracker →>"
            ),
        )
    except Exception as exc:
        client.chat_postMessage(channel=channel, text=_error_text("Failed to add note", exc))


# ---------------------------------------------------------------------------
# /track-delete — delete an application (with confirmation step)
# ---------------------------------------------------------------------------

@app.command("/track-delete")
def track_delete_command(ack, body, client, respond):
    ack()
    try:
        options = _app_options(active_only=False)  # allow deleting any record
    except Exception as exc:
        respond(_error_text("Could not load applications", exc))
        return

    if not options:
        respond("No applications found.")
        return

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "track_delete_select",
            "title": {"type": "plain_text", "text": "Delete Application"},
            "submit": {"type": "plain_text", "text": "Continue →"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": ":warning: *This will permanently delete the record and all its comments.*"},
                },
                {
                    "type": "input",
                    "block_id": "app_id",
                    "label": {"type": "plain_text", "text": "Application to delete"},
                    "element": {
                        "type": "static_select",
                        "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "Select an application…"},
                        "options": options,
                    },
                },
            ],
        },
    )


@app.view("track_delete_select")
def track_delete_select_submit(ack, body, client, view):
    """First step: show confirmation modal."""
    app_id = view["state"]["values"]["app_id"]["value"]["selected_option"]["value"]
    label  = view["state"]["values"]["app_id"]["value"]["selected_option"]["text"]["text"]

    ack({
        "response_action": "push",
        "view": {
            "type": "modal",
            "callback_id": "track_delete_confirm",
            "title": {"type": "plain_text", "text": "Confirm Delete"},
            "submit": {"type": "plain_text", "text": "Delete"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "private_metadata": app_id,
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":warning: Are you sure you want to permanently delete:\n\n*{label}*\n\nThis cannot be undone.",
                    },
                },
            ],
        },
    })


@app.view("track_delete_confirm")
def track_delete_confirm_submit(ack, body, client, view):
    ack()
    app_id  = view["private_metadata"]
    channel = body["user"]["id"]

    try:
        rec = _get_app(app_id)
        _delete_app(app_id)
        client.chat_postMessage(
            channel=channel,
            text=(
                f":wastebasket: Deleted *{rec.get('role_title')}* at *{rec.get('company')}*."
            ),
        )
    except Exception as exc:
        client.chat_postMessage(channel=channel, text=_error_text("Failed to delete", exc))


# ---------------------------------------------------------------------------
# /apply — generate resume + cover letter
# ---------------------------------------------------------------------------

def _start_apply_run(channel: str, client, company: str, role: str, contact: str,
                      job_posting: str, domain: str = "") -> None:
    def _run():
        client.chat_postMessage(
            channel=channel,
            text=f":hourglass_flowing_sand: Starting application for *{role}* at *{company}*…",
        )
        try:
            run_data = _post_run(job_posting, company, role, contact, domain)
            run_id   = run_data["run_id"]
            status   = _poll_run(run_id)
        except Exception as exc:
            client.chat_postMessage(channel=channel, text=_error_text("Error starting run", exc))
            return

        if status["status"] == "done":
            client.chat_postMessage(
                channel=channel,
                blocks=_completion_blocks(
                    ":white_check_mark: Done!", company, role, domain,
                    "Resume, ATS resume, and cover letter are in your Google Drive.\n"
                    f"<{API_BASE}|Open the app> to download the files.",
                ),
                text=f"{role} @ {company} — done!",
            )
        elif status["status"] == "timeout":
            client.chat_postMessage(
                channel=channel,
                text=f":warning: Run is taking longer than expected. Check <{API_BASE}|the app> for status.",
            )
        else:
            client.chat_postMessage(
                channel=channel,
                text=f":x: Run failed: {status.get('error', 'Unknown error')}",
            )

    threading.Thread(target=_run, daemon=True).start()


@app.command("/apply")
def apply_command(ack, body, client):
    ack()
    options = _app_options(active_only=False)
    if not options:
        client.chat_postMessage(
            channel=body["user_id"],
            text=":x: No applications on file yet. Add one with `/track-add` first.",
        )
        return
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "apply_select_submit",
            "title": {"type": "plain_text", "text": "Generate Application"},
            "submit": {"type": "plain_text", "text": "Continue"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "app_block",
                    "label": {"type": "plain_text", "text": "Application"},
                    "element": {"type": "static_select", "action_id": "app_select",
                                "placeholder": {"type": "plain_text", "text": "Select application…"},
                                "options": options},
                },
                {
                    "type": "input",
                    "block_id": "contact",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Hiring manager name (optional)"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "Jane Smith"}},
                },
            ],
        },
    )


@app.view("apply_select_submit")
def apply_select_view_submit(ack, body, client, view):
    vals    = view["state"]["values"]
    app_id  = vals["app_block"]["app_select"]["selected_option"]["value"]
    contact = (vals["contact"]["value"]["value"] or "").strip()
    channel = body["user"]["id"]

    try:
        record = _get_app(app_id)
    except Exception as exc:
        ack(response_action="errors", errors={"app_block": _error_text("Could not load application", exc)[:150]})
        return

    company     = record.get("company", "?")
    role        = record.get("role_title", "?")
    domain      = record.get("domain", "")
    job_posting = _get_saved_job_posting(record)

    if job_posting:
        ack()
        _start_apply_run(channel, client, company, role, contact, job_posting, domain)
        return

    metadata = json.dumps({"company": company, "role": role, "contact": contact, "domain": domain})
    ack(response_action="update", view=_jd_paste_view("apply_final_submit", metadata))


@app.view("apply_final_submit")
def apply_final_view_submit(ack, body, client, view):
    ack()
    meta        = json.loads(view.get("private_metadata") or "{}")
    job_posting = view["state"]["values"]["job_posting"]["value"]["value"].strip()
    channel     = body["user"]["id"]
    _start_apply_run(
        channel, client, meta.get("company", ""), meta.get("role", ""),
        meta.get("contact", ""), job_posting, meta.get("domain", ""),
    )


@app.view("apply_submit")
def apply_view_submit(ack, body, client, view):
    # Kept for any apply modal already open from before this deploy — new
    # /apply invocations use apply_select_submit/apply_final_submit instead.
    ack()
    vals        = view["state"]["values"]
    company     = vals["company"]["value"]["value"].strip()
    role        = vals["role"]["value"]["value"].strip()
    contact     = (vals["contact"]["value"]["value"] or "").strip()
    job_posting = vals["job_posting"]["value"]["value"].strip()
    channel     = body["user"]["id"]
    _start_apply_run(channel, client, company, role, contact, job_posting)


# ---------------------------------------------------------------------------
# /prep — generate interview prep doc
# ---------------------------------------------------------------------------

def _start_prep_run(channel: str, client, company: str, role: str, round_type: str,
                     interviewer: str, focus: str, job_posting: str, domain: str = "",
                     interview_date: str = "", interview_time: str = "", location: str = "") -> None:
    def _run():
        client.chat_postMessage(
            channel=channel,
            text=f":hourglass_flowing_sand: Generating *{round_type.replace('_', ' ').title()}* prep for *{role}* at *{company}*…",
        )
        try:
            prep_data = _post_prep(job_posting, company, role, round_type, focus, interviewer,
                                    interview_date, interview_time, location, domain)
            prep_id   = prep_data["prep_id"]
            status    = _poll_prep(prep_id)
        except Exception as exc:
            client.chat_postMessage(channel=channel, text=_error_text("Error starting prep", exc))
            return

        if status["status"] == "done":
            client.chat_postMessage(
                channel=channel,
                blocks=_completion_blocks(
                    f":white_check_mark: {round_type.replace('_', ' ').title()} prep ready!", company, role, domain,
                    "Your prep card is in Google Drive.\n"
                    f"<{API_BASE}|Open the app> to download it.",
                ),
                text=f"{round_type.replace('_', ' ').title()} prep for {role} @ {company} — done!",
            )
        elif status["status"] == "timeout":
            client.chat_postMessage(
                channel=channel,
                text=f":warning: Prep is taking longer than expected. Check <{API_BASE}|the app> for status.",
            )
        else:
            client.chat_postMessage(
                channel=channel,
                text=f":x: Prep failed: {status.get('error', 'Unknown error')}",
            )

    threading.Thread(target=_run, daemon=True).start()


@app.command("/prep")
def prep_command(ack, body, client):
    ack()
    options = _app_options(active_only=False)
    if not options:
        client.chat_postMessage(
            channel=body["user_id"],
            text=":x: No applications on file yet. Add one with `/track-add` first.",
        )
        return
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "prep_select_submit",
            "title": {"type": "plain_text", "text": "Interview Prep"},
            "submit": {"type": "plain_text", "text": "Continue"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "app_block",
                    "label": {"type": "plain_text", "text": "Application"},
                    "element": {"type": "static_select", "action_id": "app_select",
                                "placeholder": {"type": "plain_text", "text": "Select application…"},
                                "options": options},
                },
                {
                    "type": "input",
                    "block_id": "round_type",
                    "label": {"type": "plain_text", "text": "Round type"},
                    "element": {
                        "type": "static_select",
                        "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "Select round type"},
                        "options": [
                            {"text": {"type": "plain_text", "text": rt.replace("_", " ").title()},
                             "value": rt}
                            for rt in ROUND_TYPES
                        ],
                    },
                },
                {
                    "type": "input",
                    "block_id": "interviewer",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Interviewer(s) (optional)"},
                    "element": {"type": "plain_text_input", "action_id": "value", "multiline": True,
                                "placeholder": {"type": "plain_text",
                                                "text": "One per line, e.g. Jane Smith — VP Engineering — linkedin.com/in/janesmith"}},
                },
                {
                    "type": "input",
                    "block_id": "interview_date",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Date (optional)"},
                    "element": {"type": "datepicker", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "Select a date"}},
                },
                {
                    "type": "input",
                    "block_id": "interview_time",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Time (optional)"},
                    "element": {"type": "timepicker", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "Select a time"}},
                },
                {
                    "type": "input",
                    "block_id": "location",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Location (optional)"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "Address or video call link"}},
                },
                {
                    "type": "input",
                    "block_id": "focus",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Focus areas (optional)"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "System design, API architecture"}},
                },
            ],
        },
    )


@app.view("prep_select_submit")
def prep_select_view_submit(ack, body, client, view):
    vals           = view["state"]["values"]
    app_id         = vals["app_block"]["app_select"]["selected_option"]["value"]
    round_type     = vals["round_type"]["value"]["selected_option"]["value"]
    interviewer    = (vals["interviewer"]["value"]["value"] or "").strip()
    interview_date = (vals["interview_date"]["value"].get("selected_date") or "")
    interview_time = (vals["interview_time"]["value"].get("selected_time") or "")
    location       = (vals["location"]["value"]["value"] or "").strip()
    focus          = (vals["focus"]["value"]["value"] or "").strip()
    channel        = body["user"]["id"]

    try:
        record = _get_app(app_id)
    except Exception as exc:
        ack(response_action="errors", errors={"app_block": _error_text("Could not load application", exc)[:150]})
        return

    company     = record.get("company", "?")
    role        = record.get("role_title", "?")
    domain      = record.get("domain", "")
    job_posting = _get_saved_job_posting(record)

    if job_posting:
        ack()
        _start_prep_run(channel, client, company, role, round_type, interviewer, focus, job_posting, domain,
                         interview_date, interview_time, location)
        return

    metadata = json.dumps({
        "company": company, "role": role, "domain": domain,
        "round_type": round_type, "interviewer": interviewer, "focus": focus,
        "interview_date": interview_date, "interview_time": interview_time, "location": location,
    })
    ack(response_action="update", view=_jd_paste_view("prep_final_submit", metadata))


@app.view("prep_final_submit")
def prep_final_view_submit(ack, body, client, view):
    ack()
    meta        = json.loads(view.get("private_metadata") or "{}")
    job_posting = view["state"]["values"]["job_posting"]["value"]["value"].strip()
    channel     = body["user"]["id"]
    _start_prep_run(
        channel, client, meta.get("company", ""), meta.get("role", ""),
        meta.get("round_type", ""), meta.get("interviewer", ""), meta.get("focus", ""), job_posting,
        meta.get("domain", ""),
        meta.get("interview_date", ""), meta.get("interview_time", ""), meta.get("location", ""),
    )


@app.view("prep_submit")
def prep_view_submit(ack, body, client, view):
    # Kept for any prep modal already open from before this deploy — new
    # /prep invocations use prep_select_submit/prep_final_submit instead.
    ack()
    vals        = view["state"]["values"]
    company     = vals["company"]["value"]["value"].strip()
    role        = vals["role"]["value"]["value"].strip()
    round_type  = vals["round_type"]["value"]["selected_option"]["value"]
    interviewer = (vals["interviewer"]["value"]["value"] or "").strip()
    focus       = (vals["focus"]["value"]["value"] or "").strip()
    job_posting = vals["job_posting"]["value"]["value"].strip()
    channel     = body["user"]["id"]
    _start_prep_run(channel, client, company, role, round_type, interviewer, focus, job_posting)

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# /aq — Application Questions
# ---------------------------------------------------------------------------

def _start_aq_run(channel: str, client, company: str, role: str, question: str,
                   tone: str, char_limit: int | None, job_posting: str, domain: str = "") -> None:
    def _run():
        client.chat_postMessage(
            channel=channel,
            text=f":pencil: Answering application question for *{role}* at *{company}*…",
        )
        try:
            aq_data = _post_aq(question, job_posting, company, role, tone, char_limit)
            aq_id   = aq_data["aq_id"]

            # Poll — the agent may ask for clarification mid-run.
            # In Slack we skip the interactive clarification flow and just let
            # the agent answer with best-effort (clarification timeout → it
            # generates anyway on second pass). For interactive clarification,
            # use the web app.
            status = _poll_aq(aq_id)
        except Exception as exc:
            client.chat_postMessage(channel=channel, text=_error_text("Error", exc))
            return

        if status["status"] == "done":
            # Fetch the answer from the SSE done event — it's stored in the
            # status result on the server. We'll re-poll to get it.
            # The answer text isn't in /status, so we direct users to the app.
            client.chat_postMessage(
                channel=channel,
                blocks=_completion_blocks(
                    ":white_check_mark: Application Question Answered", company, role, domain,
                    f"Open <{API_BASE}/agents.html|the app> to view, edit, and copy your answer.",
                ),
                text=f"Application question answered for {role} @ {company}",
            )
        elif status["status"] == "timeout":
            client.chat_postMessage(
                channel=channel,
                text=f":warning: Answer is taking longer than expected. Check <{API_BASE}|the app> for status.",
            )
        else:
            client.chat_postMessage(
                channel=channel,
                text=f":x: Failed: {status.get('error', 'Unknown error')}",
            )

    threading.Thread(target=_run, daemon=True).start()


@app.command("/aq")
def aq_command(ack, body, client):
    ack()
    options = _app_options(active_only=False)
    if not options:
        client.chat_postMessage(
            channel=body["user_id"],
            text=":x: No applications on file yet. Add one with `/track-add` first.",
        )
        return
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "aq_select_submit",
            "title": {"type": "plain_text", "text": "Application Question"},
            "submit": {"type": "plain_text", "text": "Continue"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "app_block",
                    "label": {"type": "plain_text", "text": "Application"},
                    "element": {"type": "static_select", "action_id": "app_select",
                                "placeholder": {"type": "plain_text", "text": "Select application…"},
                                "options": options},
                },
                {
                    "type": "input",
                    "block_id": "question",
                    "label": {"type": "plain_text", "text": "Application question"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "multiline": True,
                                "placeholder": {"type": "plain_text", "text": "Paste the question from the application form…"}},
                },
                {
                    "type": "input",
                    "block_id": "tone",
                    "label": {"type": "plain_text", "text": "Tone"},
                    "element": {
                        "type": "static_select",
                        "action_id": "value",
                        "initial_option": {"text": {"type": "plain_text", "text": "Professional"}, "value": "professional"},
                        "options": [
                            {"text": {"type": "plain_text", "text": "Professional"}, "value": "professional"},
                            {"text": {"type": "plain_text", "text": "Conversational"}, "value": "conversational"},
                            {"text": {"type": "plain_text", "text": "Technical"}, "value": "technical"},
                            {"text": {"type": "plain_text", "text": "Concise"}, "value": "concise"},
                        ],
                    },
                },
                {
                    "type": "input",
                    "block_id": "char_limit",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Character limit (optional)"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "e.g. 500"}},
                },
            ],
        },
    )


@app.view("aq_select_submit")
def aq_select_view_submit(ack, body, client, view):
    vals       = view["state"]["values"]
    app_id     = vals["app_block"]["app_select"]["selected_option"]["value"]
    question   = vals["question"]["value"]["value"].strip()
    tone       = vals["tone"]["value"]["selected_option"]["value"]
    char_raw   = (vals["char_limit"]["value"]["value"] or "").strip()
    char_limit = int(char_raw) if char_raw.isdigit() else None
    channel    = body["user"]["id"]

    try:
        record = _get_app(app_id)
    except Exception as exc:
        ack(response_action="errors", errors={"app_block": _error_text("Could not load application", exc)[:150]})
        return

    company     = record.get("company", "?")
    role        = record.get("role_title", "?")
    domain      = record.get("domain", "")
    job_posting = _get_saved_job_posting(record)

    if job_posting:
        ack()
        _start_aq_run(channel, client, company, role, question, tone, char_limit, job_posting, domain)
        return

    metadata = json.dumps({
        "company": company, "role": role, "domain": domain, "question": question,
        "tone": tone, "char_limit": char_limit,
    })
    ack(response_action="update", view=_jd_paste_view("aq_final_submit", metadata))


@app.view("aq_final_submit")
def aq_final_view_submit(ack, body, client, view):
    ack()
    meta        = json.loads(view.get("private_metadata") or "{}")
    job_posting = view["state"]["values"]["job_posting"]["value"]["value"].strip()
    channel     = body["user"]["id"]
    _start_aq_run(
        channel, client, meta.get("company", ""), meta.get("role", ""),
        meta.get("question", ""), meta.get("tone", "professional"), meta.get("char_limit"), job_posting,
        meta.get("domain", ""),
    )


@app.view("aq_submit")
def aq_view_submit(ack, body, client, view):
    # Kept for any aq modal already open from before this deploy — new /aq
    # invocations use aq_select_submit/aq_final_submit instead.
    ack()
    vals        = view["state"]["values"]
    company     = vals["company"]["value"]["value"].strip()
    role        = vals["role"]["value"]["value"].strip()
    question    = vals["question"]["value"]["value"].strip()
    tone        = vals["tone"]["value"]["selected_option"]["value"]
    char_raw    = (vals["char_limit"]["value"]["value"] or "").strip()
    char_limit  = int(char_raw) if char_raw.isdigit() else None
    job_posting = vals["job_posting"]["value"]["value"].strip()
    channel     = body["user"]["id"]
    _start_aq_run(channel, client, company, role, question, tone, char_limit, job_posting)


# ---------------------------------------------------------------------------
# /thankyou — generate a post-interview thank-you email
# ---------------------------------------------------------------------------

THANKYOU_ROUND_TYPES = ["Phone Screen", "Hiring Manager", "Peer", "Technical", "Executive", "Panel"]

@app.command("/thankyou")
def thankyou_command(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "thankyou_submit",
            "title": {"type": "plain_text", "text": "Thank You Email"},
            "submit": {"type": "plain_text", "text": "Generate"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "company",
                    "label": {"type": "plain_text", "text": "Company name"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "Acme Corp"}},
                },
                {
                    "type": "input",
                    "block_id": "role",
                    "label": {"type": "plain_text", "text": "Role title"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "Solutions Engineer"}},
                },
                {
                    "type": "input",
                    "block_id": "round_type",
                    "label": {"type": "plain_text", "text": "Interview round"},
                    "element": {
                        "type": "static_select",
                        "action_id": "value",
                        "initial_option": {"text": {"type": "plain_text", "text": "Hiring Manager"}, "value": "Hiring Manager"},
                        "options": [
                            {"text": {"type": "plain_text", "text": rt}, "value": rt}
                            for rt in THANKYOU_ROUND_TYPES
                        ],
                    },
                },
                {
                    "type": "input",
                    "block_id": "tone",
                    "label": {"type": "plain_text", "text": "Tone"},
                    "element": {
                        "type": "static_select",
                        "action_id": "value",
                        "initial_option": {"text": {"type": "plain_text", "text": "Professional"}, "value": "professional"},
                        "options": [
                            {"text": {"type": "plain_text", "text": "Professional"}, "value": "professional"},
                            {"text": {"type": "plain_text", "text": "Conversational"}, "value": "conversational"},
                            {"text": {"type": "plain_text", "text": "Concise"}, "value": "concise"},
                        ],
                    },
                },
                {
                    "type": "input",
                    "block_id": "interviewer",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Interviewer name(s) (optional)"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "placeholder": {"type": "plain_text", "text": "Sarah Chen, VP Engineering"}},
                },
                {
                    "type": "input",
                    "block_id": "topics",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Key topics discussed (optional)"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "multiline": True,
                                "placeholder": {"type": "plain_text", "text": "e.g. MCP integration roadmap, scaling connectors, team culture"}},
                },
                {
                    "type": "input",
                    "block_id": "job_posting",
                    "label": {"type": "plain_text", "text": "Job posting (paste full text)"},
                    "element": {"type": "plain_text_input", "action_id": "value",
                                "multiline": True,
                                "placeholder": {"type": "plain_text", "text": "Paste the full job description here…"}},
                },
            ],
        },
    )


@app.view("thankyou_submit")
def thankyou_view_submit(ack, body, client, view):
    ack()
    vals        = view["state"]["values"]
    company     = vals["company"]["value"]["value"].strip()
    role        = vals["role"]["value"]["value"].strip()
    round_type  = vals["round_type"]["value"]["selected_option"]["value"]
    tone        = vals["tone"]["value"]["selected_option"]["value"]
    interviewer = (vals["interviewer"]["value"]["value"] or "").strip()
    topics      = (vals["topics"]["value"]["value"] or "").strip()
    job_posting = vals["job_posting"]["value"]["value"].strip()
    channel     = body["user"]["id"]

    def _run():
        client.chat_postMessage(
            channel=channel,
            text=f":hourglass_flowing_sand: Generating thank-you email for *{role}* at *{company}* ({round_type})…",
        )
        try:
            ty_data = _post_thankyou(job_posting, company, role, round_type,
                                     interviewer, topics, tone)
            ty_id   = ty_data["ty_id"]
            status  = _poll_thankyou(ty_id)
        except Exception as exc:
            client.chat_postMessage(channel=channel, text=_error_text("Error", exc))
            return

        if status["status"] == "done":
            client.chat_postMessage(
                channel=channel,
                blocks=_completion_blocks(
                    f":white_check_mark: Thank-You Email Ready ({round_type})", company, role, "",
                    "Your email and DOCX are in Google Drive.\n"
                    f"Open <{API_BASE}/agents.html|the app> to view, edit, and copy.",
                ),
                text=f"Thank-you email ready for {role} @ {company}",
            )
        elif status["status"] == "timeout":
            client.chat_postMessage(
                channel=channel,
                text=f":warning: Taking longer than expected. Check <{API_BASE}|the app> for status.",
            )
        else:
            client.chat_postMessage(
                channel=channel,
                text=f":x: Failed: {status.get('error', 'Unknown error')}",
            )

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# /whoami — account info
# ---------------------------------------------------------------------------

@app.command("/whoami")
def me_command(ack, respond):
    ack()
    try:
        r = _api("get", "/api/auth/me")
        r.raise_for_status()
        u = r.json()
    except Exception as exc:
        respond(_error_text("Could not load account", exc))
        return

    header = f":bust_in_silhouette: {u.get('display_name') or u.get('email')}"
    pairs = [
        ("Email", u.get("email", "?")),
        ("Role", u.get("role", "user").capitalize()),
        ("Email Verified", "✅ Yes" if u.get("email_verified", True) else "❌ No"),
        ("Master Resume", "✅ Uploaded" if u.get("has_resume") else "❌ Not uploaded"),
        ("Profile Guide", "✅ Yes" if u.get("has_profile") else "❌ Not set"),
    ]
    respond(blocks=_fields_blocks(header, pairs), text=header)


# ---------------------------------------------------------------------------
# /runs — recent agent run folders from Drive
# ---------------------------------------------------------------------------

@app.command("/runs")
def runs_command(ack, respond):
    ack()
    try:
        r = _api("get", "/api/gdrive/runs")
        r.raise_for_status()
        runs = r.json().get("runs", [])
    except Exception as exc:
        respond(_error_text("Could not load runs", exc))
        return

    if not runs:
        respond("No agent runs found in Drive yet.")
        return

    shown = runs[:10]
    header = f":file_folder: *Recent Agent Runs* ({len(runs)} total)"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}]

    for run in shown:
        name    = run.get("name", "")
        idx     = name.find("_")
        company = name[:idx] if idx > 0 else name
        label   = f"{company} · {name[idx+1:].replace('_',' ')}" if idx > 0 else name
        link    = run.get("web_view_link", "")
        badge   = " ↩" if run.get("source") == "legacy" else ""
        text    = f"• {'<' + link + '|' + label + '>' if link else label}{badge}"
        elements = []
        logo_el = _logo_element(run.get("domain", ""), alt_text=company)
        if logo_el:
            elements.append(logo_el)
        elements.append({"type": "mrkdwn", "text": text})
        blocks.append({"type": "context", "elements": elements})

    if len(runs) > 10:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": f"_…and {len(runs) - 10} more — <{API_BASE}|open the app> to see all._"}],
        })

    respond(blocks=blocks, text=header)


# ---------------------------------------------------------------------------
# /company — Logo.dev company lookup
# ---------------------------------------------------------------------------

@app.command("/company")
def company_command(ack, respond, body):
    ack()
    query = body.get("text", "").strip()
    if not query:
        respond("Usage: `/company [company name]`\nExample: `/company Salesforce`")
        return
    try:
        r = _api("get", "/api/companies/search", params={"q": query})
        r.raise_for_status()
        results = r.json()
    except Exception as exc:
        respond(_error_text("Search failed", exc))
        return

    if not results:
        respond(f":mag: No results found for *{query}*.")
        return

    header = f":mag: *Company search: {query}*"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}]
    for c in results[:5]:
        name   = c.get("name", "?")
        domain = c.get("domain", "")
        desc   = c.get("description", "")
        line   = f"*{name}*"
        if domain:
            line += f"  `{domain}`"
        if desc:
            line += f"\n_{desc}_"
        elements = []
        logo_el = _logo_element(domain, alt_text=name)
        if logo_el:
            elements.append(logo_el)
        elements.append({"type": "mrkdwn", "text": line})
        blocks.append({"type": "context", "elements": elements})

    respond(blocks=blocks, text=header)


# ---------------------------------------------------------------------------
# /track-view — view full application details
# ---------------------------------------------------------------------------

@app.command("/track-view")
def track_view_command(ack, body, client, respond):
    ack()
    try:
        options = _app_options()
    except Exception as exc:
        respond(_error_text("Could not load applications", exc))
        return

    if not options:
        respond("No applications found. Use `/track-add` to create one.")
        return

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "track_view_submit",
            "title": {"type": "plain_text", "text": "View Application"},
            "submit": {"type": "plain_text", "text": "View"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "app_id",
                    "label": {"type": "plain_text", "text": "Application"},
                    "element": {
                        "type": "static_select",
                        "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "Select an application…"},
                        "options": options,
                    },
                },
            ],
        },
    )


@app.view("track_view_submit")
def track_view_view_submit(ack, body, client, view):
    ack()
    app_id  = view["state"]["values"]["app_id"]["value"]["selected_option"]["value"]
    channel = body["user"]["id"]
    try:
        a = _get_app(app_id)
        header_elements = []
        logo_el = _logo_element(a.get("domain", ""), alt_text=a.get("company", ""))
        if logo_el:
            header_elements.append(logo_el)
        header_elements.append({
            "type": "mrkdwn", "text": f":briefcase: *{a.get('company')} — {a.get('role_title')}*",
        })
        header_block = {"type": "context", "elements": header_elements}

        lines = [f"• Status: `{a.get('status')}`"]
        if a.get("date_applied"):
            lines.append(f"• Applied: {a['date_applied'][:10]}")
        if a.get("location"):
            lines.append(f"• Location: {a['location']}")
        if a.get("salary_range"):
            lines.append(f"• Salary: {a['salary_range']}")
        if a.get("recruiter_name"):
            rec = a["recruiter_name"]
            if a.get("recruiter_email"):
                rec += f" <{a['recruiter_email']}>"
            lines.append(f"• Recruiter: {rec}")
        if a.get("url"):
            lines.append(f"• <{a['url']}|Job Posting ↗>")
        drive_runs = [r for r in (a.get("linked_runs") or []) if r.get("folder_url")]
        if drive_runs:
            drive_runs.sort(key=lambda r: r.get("linked_at", ""), reverse=True)
            lines.append(f"• <{drive_runs[0]['folder_url']}|Drive Folder ↗>")
        comments = a.get("comments", [])
        if comments:
            lines.append(f"\n:speech_balloon: *Notes ({len(comments)}):*")
            for c in comments[-3:]:
                lines.append(f"  › {c['text'][:120]}")

        blocks = [header_block, {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]
        fallback = f"{a.get('company')} — {a.get('role_title')}\n" + "\n".join(lines)
        client.chat_postMessage(channel=channel, blocks=blocks, text=fallback)
    except Exception as exc:
        client.chat_postMessage(channel=channel, text=_error_text("Could not load application", exc))


# ---------------------------------------------------------------------------
# Profile commands
# ---------------------------------------------------------------------------

@app.command("/profile-resume")
def profile_resume_command(ack, respond):
    ack()
    respond(
        ":paperclip: *Upload your master resume*\n\n"
        "To update your resume on file:\n"
        "1. Upload a `.docx` file directly to this DM channel with the Slack bot\n"
        "2. In the file caption / message, type `resume`\n\n"
        "The bot will automatically detect and save it as your new master resume."
    )


@app.event("message")
def handle_message_with_file(body, client, logger):
    """Detect .docx file uploads in DMs and save as master resume when caption contains 'resume'."""
    event = body.get("event", {})
    # Only process DMs
    if event.get("channel_type") != "im":
        return
    files = event.get("files", [])
    text  = (event.get("text") or "").lower()
    if not files or "resume" not in text:
        return

    docx_files = [f for f in files if f.get("name", "").lower().endswith(".docx")]
    if not docx_files:
        return

    f        = docx_files[0]
    user_id  = event["user"]
    dl_url   = f.get("url_private_download") or f.get("url_private")

    try:
        # Download file from Slack — use url_private with bot token
        # Slack may return HTML if the bot lacks files:read scope or the URL has changed.
        # Try both url_private_download and url_private with proper auth.
        file_bytes = None
        for url in [f.get("url_private_download"), f.get("url_private")]:
            if not url or not _is_trusted_slack_file_host(url):
                continue
            dl = requests.get(url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}, timeout=30)
            dl.raise_for_status()
            if dl.content[:4] == b"PK\x03\x04":
                file_bytes = dl.content
                break
            logger.warning(f"Slack URL {url[:80]} returned non-ZIP: "
                           f"content_type={dl.headers.get('Content-Type')}, "
                           f"size={len(dl.content)}, first_bytes={dl.content[:40]!r}")

        if file_bytes is None:
            logger.error("All Slack download URLs returned non-ZIP content. "
                         "Check that the bot has files:read scope.")
            client.chat_postMessage(
                channel=user_id,
                text=(f":warning: *{f['name']}* couldn't be downloaded from Slack. "
                      "This may be a Slack permissions issue — please upload directly "
                      "at https://apply.cdlav.us/profile.html"),
            )
            return

        # Upload to API
        r = _api("post", "/api/profile/resume",
                 files={"resume": (f["name"], file_bytes,
                                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        r.raise_for_status()
        client.chat_postMessage(
            channel=user_id,
            text=f":white_check_mark: Resume *{f['name']}* saved as your master resume.",
        )
    except requests.exceptions.HTTPError as exc:
        detail = ""
        try:
            detail = exc.response.json().get("detail", "")
        except Exception:
            detail = exc.response.text[:200] if exc.response is not None else ""
        logger.error(f"Resume upload failed: {exc} — detail: {detail}")
        client.chat_postMessage(channel=user_id, text=_error_text("Failed to save resume", detail or exc))
    except Exception as exc:
        logger.error(f"Resume upload failed: {exc}")
        client.chat_postMessage(channel=user_id, text=_error_text("Failed to save resume", exc))


@app.command("/profile-guide")
def profile_guide_command(ack, body, client):
    ack()
    # Pre-fill with existing guide text
    existing = ""
    try:
        r = _api("get", "/api/profile")
        if r.ok:
            existing = r.json().get("profile_text", "") or ""
    except Exception:
        pass

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "profile_guide_submit",
            "title": {"type": "plain_text", "text": "Profile & Voice Guide"},
            "submit": {"type": "plain_text", "text": "Save"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Your profile guide tells the AI how to write in your voice — tone, stories, phrases to avoid, and context about your background.",
                    },
                },
                {
                    "type": "input",
                    "block_id": "guide_block",
                    "label": {"type": "plain_text", "text": "Profile & Voice Guide"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "guide_input",
                        "multiline": True,
                        "initial_value": existing[:3000],  # Slack modal limit
                        "placeholder": {"type": "plain_text", "text": "Describe your voice, tone, key stories, phrases to avoid…"},
                    },
                },
            ],
        },
    )


@app.view("profile_guide_submit")
def profile_guide_submit(ack, body, client):
    ack()
    guide   = body["view"]["state"]["values"]["guide_block"]["guide_input"]["value"]
    user_id = body["user"]["id"]
    try:
        r = _api("put", "/api/profile", json={"profile_text": guide})
        r.raise_for_status()
        client.chat_postMessage(channel=user_id, text=":white_check_mark: Profile & voice guide saved.")
    except Exception as exc:
        client.chat_postMessage(channel=user_id, text=_error_text("Failed to save guide", exc))


# ---------------------------------------------------------------------------
# /optimize — refine an existing run's documents from a prompt
# ---------------------------------------------------------------------------

def _poll_optimize(optimize_id: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api("get", f"/api/optimize/{optimize_id}/status")
        r.raise_for_status()
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(5)
    return {"status": "timeout", "error": "Timed out waiting for optimize to complete"}


@app.command("/optimize")
def optimize_command(ack, body, client):
    ack()
    options = _app_options(active_only=True)
    if not options:
        client.chat_postMessage(
            channel=body["user_id"],
            text=":x: No active applications found. Add one with `/track-add` first.",
        )
        return

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "optimize_submit",
            "title": {"type": "plain_text", "text": "Optimize Run"},
            "submit": {"type": "plain_text", "text": "Optimize"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Refine the documents from an existing agent run. The bot will look up the most recent Drive run folder for the selected application.",
                    },
                },
                {
                    "type": "input",
                    "block_id": "app_block",
                    "label": {"type": "plain_text", "text": "Application"},
                    "element": {
                        "type": "static_select",
                        "action_id": "app_select",
                        "placeholder": {"type": "plain_text", "text": "Select application…"},
                        "options": options,
                    },
                },
                {
                    "type": "input",
                    "block_id": "instruction_block",
                    "label": {"type": "plain_text", "text": "Optimization prompt"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "instruction_input",
                        "multiline": True,
                        "placeholder": {"type": "plain_text",
                                        "text": "e.g. Strengthen the eHealth bullets to emphasize platform scalability. Tighten the cover letter opening."},
                    },
                },
                {
                    "type": "input",
                    "block_id": "docs_block",
                    "label": {"type": "plain_text", "text": "Documents to optimize"},
                    "element": {
                        "type": "checkboxes",
                        "action_id": "docs_input",
                        "initial_options": [
                            {"text": {"type": "plain_text", "text": "Resume"}, "value": "resume"},
                            {"text": {"type": "plain_text", "text": "Cover letter"}, "value": "cover_letter"},
                        ],
                        "options": [
                            {"text": {"type": "plain_text", "text": "Resume"}, "value": "resume"},
                            {"text": {"type": "plain_text", "text": "Cover letter"}, "value": "cover_letter"},
                        ],
                    },
                },
            ],
        },
    )


@app.view("optimize_submit")
def optimize_view_submit(ack, body, client, view):
    ack()
    vals        = view["state"]["values"]
    app_id      = vals["app_block"]["app_select"]["selected_option"]["value"]
    instruction = vals["instruction_block"]["instruction_input"]["value"].strip()
    selected    = [o["value"] for o in (vals["docs_block"]["docs_input"].get("selected_options") or [])]
    channel     = body["user"]["id"]

    def _run():
        try:
            record = _get_app(app_id)
        except Exception as exc:
            client.chat_postMessage(channel=channel, text=_error_text("Could not load application", exc))
            return

        company = record.get("company", "?")
        role    = record.get("role_title", "?")

        runs = [r for r in (record.get("linked_runs") or []) if r.get("gdrive_folder_id")]
        if not runs:
            client.chat_postMessage(
                channel=channel,
                text=f":x: *{role} @ {company}* has no linked Drive run folder. Run `/apply` for this application first.",
            )
            return

        runs.sort(key=lambda r: r.get("linked_at", ""), reverse=True)
        preferred = next((r for r in runs if r.get("type") in ("resume", "optimize")), None)
        folder_id = (preferred or runs[0])["gdrive_folder_id"]

        client.chat_postMessage(
            channel=channel,
            text=f":hourglass_flowing_sand: Optimizing *{role}* @ *{company}*…",
        )
        try:
            r = _api("post", "/api/optimize", json={
                "app_id": app_id,
                "folder_id": folder_id,
                "instruction": instruction,
                "company": company,
                "role": role,
                "optimize_resume": "resume" in selected,
                "optimize_cover_letter": "cover_letter" in selected,
            })
            r.raise_for_status()
            optimize_id = r.json()["optimize_id"]
        except Exception as exc:
            client.chat_postMessage(channel=channel, text=_error_text("Failed to start optimization", exc))
            return

        status = _poll_optimize(optimize_id)
        if status["status"] == "done":
            client.chat_postMessage(
                channel=channel,
                blocks=_completion_blocks(
                    ":white_check_mark: Optimization Complete!", company, role, record.get("domain", ""),
                    "Updated documents are in your Google Drive run folder.\n"
                    f"<{API_BASE}|Open the app> to download the files.",
                ),
                text=f"{role} @ {company} — optimization complete!",
            )
        elif status["status"] == "timeout":
            client.chat_postMessage(
                channel=channel,
                text=f":warning: Optimization is taking longer than expected. Check <{API_BASE}|the app> for status.",
            )
        else:
            client.chat_postMessage(
                channel=channel,
                text=f":x: Optimization failed: {status.get('error', 'Unknown error')}",
            )

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# /rescore — re-score resume/JD match for an application
# ---------------------------------------------------------------------------

@app.command("/rescore")
def rescore_command(ack, body, client):
    ack()
    options = _app_options(active_only=True)
    if not options:
        client.chat_postMessage(
            channel=body["user_id"],
            text=":x: No active applications found.",
        )
        return

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "rescore_submit",
            "title": {"type": "plain_text", "text": "Rescore Match"},
            "submit": {"type": "plain_text", "text": "Rescore"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Re-score how well your resume matches this application's job posting. Requires a linked job description.",
                    },
                },
                {
                    "type": "input",
                    "block_id": "app_block",
                    "label": {"type": "plain_text", "text": "Application"},
                    "element": {
                        "type": "static_select",
                        "action_id": "app_select",
                        "placeholder": {"type": "plain_text", "text": "Select application…"},
                        "options": options,
                    },
                },
            ],
        },
    )


@app.view("rescore_submit")
def rescore_view_submit(ack, body, client, view):
    ack()
    app_id  = view["state"]["values"]["app_block"]["app_select"]["selected_option"]["value"]
    channel = body["user"]["id"]

    def _run():
        try:
            record = _get_app(app_id)
            company = record.get("company", "?")
            role    = record.get("role_title", "?")
            client.chat_postMessage(
                channel=channel,
                text=f":hourglass_flowing_sand: Scoring *{role}* @ *{company}*…",
            )
            r = _api("post", f"/api/applications/{app_id}/score")
            r.raise_for_status()
            result = r.json()
        except Exception as exc:
            msg = ""
            if hasattr(exc, "response") and exc.response is not None:
                try:
                    msg = exc.response.json().get("detail", str(exc))
                except Exception:
                    msg = str(exc)
            else:
                msg = str(exc)
            client.chat_postMessage(channel=channel, text=f":x: Rescore failed: {msg}")
            return

        score     = result.get("score", "?")
        category  = result.get("category", "?")
        rationale = result.get("rationale", "")
        emoji     = ":large_green_circle:" if category == "strong" else (
                    ":large_yellow_circle:" if category == "good" else ":red_circle:")

        blocks = _fields_blocks(
            f"{emoji} Match Score", [("Score", f"{score}/100"), ("Category", str(category).capitalize())],
        )
        logo_el = _logo_element(record.get("domain", ""), alt_text=company)
        subtitle = ([logo_el] if logo_el else []) + [{"type": "mrkdwn", "text": f"{company} — {role}"}]
        blocks.insert(1, {"type": "context", "elements": subtitle})
        if rationale:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"_{rationale}_"}})

        client.chat_postMessage(channel=channel, blocks=blocks, text=f"{company} · {role} — score {score}/100")

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# /notifications — view and toggle email notification preferences
# ---------------------------------------------------------------------------

_NOTIF_LABELS = {
    "researching_nudge":  "Researching nudge — remind me when an app stays in Researching too long",
    "follow_up_reminder": "Follow-up reminder — nudge me to follow up after applying",
    "gone_silent":        "Gone silent — alert when a company has not responded in a while",
    "status_changed":     "Status changed — email on every status update",
    "new_application":    "New application — email when a new app is added",
    "daily_digest":       "Daily digest — one summary email each morning",
    "weekly_digest":      "Weekly digest — one summary email each Sunday",
}


@app.command("/notifications")
def notifications_command(ack, body, client):
    ack()
    prefs = {}
    try:
        r = _api("get", "/api/profile")
        if r.ok:
            prefs = r.json().get("notification_prefs", {})
    except Exception:
        pass

    initial = [
        {"text": {"type": "plain_text", "text": label}, "value": key}
        for key, label in _NOTIF_LABELS.items()
        if prefs.get(key, True)
    ]
    all_options = [
        {"text": {"type": "plain_text", "text": label}, "value": key}
        for key, label in _NOTIF_LABELS.items()
    ]

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "notifications_submit",
            "title": {"type": "plain_text", "text": "Email Notifications"},
            "submit": {"type": "plain_text", "text": "Save"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Choose which email notifications you want to receive.",
                    },
                },
                {
                    "type": "input",
                    "block_id": "prefs_block",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Enabled notifications"},
                    "element": {
                        "type": "checkboxes",
                        "action_id": "prefs_input",
                        "initial_options": initial or None,
                        "options": all_options,
                    },
                },
            ],
        },
    )


@app.view("notifications_submit")
def notifications_view_submit(ack, body, client, view):
    ack()
    selected = {
        o["value"]
        for o in (view["state"]["values"]["prefs_block"]["prefs_input"].get("selected_options") or [])
    }
    prefs   = {key: (key in selected) for key in _NOTIF_LABELS}
    user_id = body["user"]["id"]
    try:
        r = _api("put", "/api/profile", json={"notification_prefs": prefs})
        r.raise_for_status()
        enabled  = [_NOTIF_LABELS[k] for k, v in prefs.items() if v]
        disabled = [_NOTIF_LABELS[k] for k, v in prefs.items() if not v]
        lines = [":white_check_mark: *Notification preferences saved.*"]
        if enabled:
            lines.append("*On:* " + ", ".join(k.split(" —")[0] for k in enabled))
        if disabled:
            lines.append("*Off:* " + ", ".join(k.split(" —")[0] for k in disabled))
        client.chat_postMessage(channel=user_id, text="\n".join(lines))
    except Exception as exc:
        client.chat_postMessage(channel=user_id, text=_error_text("Failed to save preferences", exc))


# ---------------------------------------------------------------------------
# Auto model upgrade scheduler
# ---------------------------------------------------------------------------

_MODEL_CHECK_INTERVAL = 6 * 60 * 60  # 6 hours
_NOTIFY_CHANNEL       = os.environ.get("SLACK_NOTIFY_CHANNEL", "")  # DM user ID or channel
_ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")


def _latest_claude_sonnet() -> str | None:
    """Query Anthropic's models API and return the newest claude-sonnet model ID."""
    if not _ANTHROPIC_API_KEY:
        return None
    try:
        resp = requests.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": _ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
            timeout=10,
        )
        resp.raise_for_status()
        models = resp.json().get("data", [])
        sonnet_ids = [
            m["id"] for m in models
            if re.search(r"claude-\S*sonnet", m["id"], re.IGNORECASE)
        ]
        if not sonnet_ids:
            return None
        # Sort by model ID descending — Anthropic uses date-embedded IDs like claude-sonnet-5
        sonnet_ids.sort(reverse=True)
        return sonnet_ids[0]
    except Exception:
        return None


def _model_check_loop():
    """Background thread: periodically check for newer Claude Sonnet models and upgrade."""
    time.sleep(60)  # delay initial check so bot finishes starting up
    while True:
        try:
            latest = _latest_claude_sonnet()
            if latest:
                r = requests.get(
                    f"{API_BASE}/api/config/model",
                    headers={"Authorization": f"Bearer {BOT_API_KEY}"},
                    timeout=10,
                )
                if r.ok:
                    current = r.json().get("model", "")
                    if latest != current:
                        # Upgrade
                        up = requests.put(
                            f"{API_BASE}/api/config/model",
                            headers={"Authorization": f"Bearer {BOT_API_KEY}"},
                            json={"model": latest},
                            timeout=10,
                        )
                        if up.ok and _NOTIFY_CHANNEL:
                            app.client.chat_postMessage(
                                channel=_NOTIFY_CHANNEL,
                                text=(
                                    f":sparkles: *Claude model auto-upgraded*\n"
                                    f"• Previous: `{current}`\n"
                                    f"• New: `{latest}`"
                                ),
                            )
        except Exception:
            pass
        time.sleep(_MODEL_CHECK_INTERVAL)


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------

CALENDAR_URL = f"{API_BASE}/calendar.html"

EVENT_TYPE_LABELS = {
    "interview":      "Interview",
    "phone_screen":   "Phone Screen",
    "deadline":       "Deadline",
    "follow_up":      "Follow-Up",
    "offer_deadline": "Offer Deadline",
    "prep":           "Prep",
    "custom":         "Custom",
}
EVENT_TYPE_EMOJI = {
    "interview":      "🎯",
    "phone_screen":   "📞",
    "deadline":       "⏰",
    "follow_up":      "📬",
    "offer_deadline": "🟣",
    "prep":           "📚",
    "custom":         "📅",
}
VALID_EVENT_TYPES = list(EVENT_TYPE_LABELS.keys())


def _get_events(from_dt: str | None = None, to_dt: str | None = None) -> list[dict]:
    params = {}
    if from_dt:
        params["from"] = from_dt
    if to_dt:
        params["to"] = to_dt
    r = _api("get", "/api/calendar", params=params)
    r.raise_for_status()
    return r.json().get("events", [])


def _get_upcoming_events() -> list[dict]:
    r = _api("get", "/api/calendar/upcoming")
    r.raise_for_status()
    return r.json().get("events", [])


def _create_cal_event(payload: dict) -> dict:
    r = _api("post", "/api/calendar", json=payload)
    r.raise_for_status()
    return r.json()


def _delete_cal_event(event_id: str) -> None:
    r = _api("delete", f"/api/calendar/{event_id}")
    r.raise_for_status()


def _fmt_event_dt(iso: str) -> str:
    if not iso:
        return "—"
    try:
        import datetime as _dt
        d = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        # %-d / %-I are Linux-only; strip the leading zero manually for portability
        day  = str(d.day)
        hour = d.hour % 12 or 12
        minute = f"{d.minute:02d}"
        ampm = "AM" if d.hour < 12 else "PM"
        return f"{d.strftime('%a %b')}{day}, {hour}:{minute} {ampm} UTC"
    except Exception:
        return iso[:16].replace("T", " ") + " UTC"


def _event_line(ev: dict) -> str:
    emoji  = EVENT_TYPE_EMOJI.get(ev.get("event_type", ""), "📅")
    title  = ev.get("title", "?")
    dt     = _fmt_event_dt(ev.get("datetime", ""))
    return f"{emoji} *{title}*  ·  {dt}"


def _cal_event_options(events: list[dict] | None = None) -> list[dict]:
    if events is None:
        events = _get_events()
    events = sorted(events, key=lambda e: e.get("datetime", ""))
    options = []
    for ev in events[:100]:
        label = f"{ev.get('title','?')} — {_fmt_event_dt(ev.get('datetime',''))}"
        if len(label) > 75:
            label = label[:72] + "…"
        options.append({
            "text":  {"type": "plain_text", "text": label},
            "value": ev["id"],
        })
    return options


# ---------------------------------------------------------------------------
# /cal-today — events today
# ---------------------------------------------------------------------------

@app.command("/cal-today")
def cal_today_command(ack, respond):
    ack()
    import datetime as _dt
    today = _dt.date.today()
    from_dt = f"{today}T00:00:00Z"
    to_dt   = f"{today}T23:59:59Z"
    try:
        events = _get_events(from_dt=from_dt, to_dt=to_dt)
    except Exception as exc:
        respond(_error_text("Could not load calendar", exc))
        return

    if not events:
        respond(f":calendar: No events today. <{CALENDAR_URL}|Open Calendar →>")
        return

    header = f":calendar: *Today — {today.strftime('%A, %B %-d')}* ({len(events)} event{'s' if len(events) != 1 else ''})"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}]
    for ev in events:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _event_line(ev)}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{CALENDAR_URL}|Open Calendar →>"}]})
    respond(blocks=blocks, text=header)


# ---------------------------------------------------------------------------
# /cal-week — next 7 days
# ---------------------------------------------------------------------------

@app.command("/cal-week")
def cal_week_command(ack, respond):
    ack()
    try:
        events = _get_upcoming_events()
    except Exception as exc:
        respond(_error_text("Could not load calendar", exc))
        return

    if not events:
        respond(f":calendar: No events in the next 7 days. <{CALENDAR_URL}|Open Calendar →>")
        return

    header = f":calendar: *Upcoming — Next 7 Days* ({len(events)} event{'s' if len(events) != 1 else ''})"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}]
    for ev in events[:20]:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _event_line(ev)}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{CALENDAR_URL}|Open Calendar →>"}]})
    respond(blocks=blocks, text=header)


# ---------------------------------------------------------------------------
# /cal-add — create a calendar event (modal)
# ---------------------------------------------------------------------------

def _cal_add_blocks() -> list:
    def _sel_opt(val, label):
        return {"text": {"type": "plain_text", "text": label}, "value": val}

    # Linked application options
    app_opts = [{"text": {"type": "plain_text", "text": "— None —"}, "value": "none"}]
    try:
        apps = _get_apps()
        for a in apps[:99]:
            label = f"{a.get('company','?')} — {a.get('role_title','?')}"
            if len(label) > 75:
                label = label[:72] + "…"
            app_opts.append({
                "text":  {"type": "plain_text", "text": label},
                "value": a["id"],
            })
    except Exception:
        pass

    return [
        {
            "type": "input", "block_id": "title",
            "label": {"type": "plain_text", "text": "Event Title"},
            "element": {"type": "plain_text_input", "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "e.g. HM Interview — Salesforce"}},
        },
        {
            "type": "input", "block_id": "event_type",
            "label": {"type": "plain_text", "text": "Event Type"},
            "element": {
                "type": "static_select", "action_id": "value",
                "initial_option": _sel_opt("interview", "Interview"),
                "options": [_sel_opt(k, v) for k, v in EVENT_TYPE_LABELS.items()],
            },
        },
        {
            "type": "input", "block_id": "event_date",
            "label": {"type": "plain_text", "text": "Date"},
            "element": {"type": "datepicker", "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "Select date"}},
        },
        {
            "type": "input", "block_id": "event_time",
            "label": {"type": "plain_text", "text": "Time (HH:MM, 24h)"},
            "hint":  {"type": "plain_text", "text": "e.g. 14:00 for 2:00 PM in the timezone below"},
            "element": {"type": "plain_text_input", "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "14:00"},
                        "initial_value": "09:00"},
        },
        {
            "type": "input", "block_id": "event_tz",
            "label": {"type": "plain_text", "text": "Timezone"},
            "hint":  {"type": "plain_text", "text": "IANA timezone name, e.g. America/New_York, America/Los_Angeles, Europe/London"},
            "element": {"type": "plain_text_input", "action_id": "value",
                        "initial_value": "America/New_York",
                        "placeholder": {"type": "plain_text", "text": "America/New_York"}},
        },
        {
            "type": "input", "block_id": "duration",
            "optional": True,
            "label": {"type": "plain_text", "text": "Duration (minutes)"},
            "element": {"type": "plain_text_input", "action_id": "value",
                        "initial_value": "60",
                        "placeholder": {"type": "plain_text", "text": "60"}},
        },
        {
            "type": "input", "block_id": "app_link",
            "optional": True,
            "label": {"type": "plain_text", "text": "Linked Application"},
            "element": {
                "type": "static_select", "action_id": "value",
                "initial_option": {"text": {"type": "plain_text", "text": "— None —"}, "value": "none"},
                "options": app_opts,
            },
        },
        {
            "type": "input", "block_id": "reminder_offset",
            "optional": True,
            "label": {"type": "plain_text", "text": "Remind me (minutes before)"},
            "hint":  {"type": "plain_text", "text": "0 = at event time. 60 = 1h before. 1440 = 1 day before."},
            "element": {"type": "plain_text_input", "action_id": "value",
                        "initial_value": "1440",
                        "placeholder": {"type": "plain_text", "text": "1440"}},
        },
        {
            "type": "input", "block_id": "reminder_channels",
            "optional": True,
            "label": {"type": "plain_text", "text": "Reminder Channels"},
            "element": {
                "type": "checkboxes", "action_id": "value",
                "initial_options": [
                    {"text": {"type": "plain_text", "text": "Email"}, "value": "email"},
                    {"text": {"type": "plain_text", "text": "Slack"}, "value": "slack"},
                ],
                "options": [
                    {"text": {"type": "plain_text", "text": "Email"}, "value": "email"},
                    {"text": {"type": "plain_text", "text": "Slack"}, "value": "slack"},
                ],
            },
        },
        {
            "type": "input", "block_id": "notes",
            "optional": True,
            "label": {"type": "plain_text", "text": "Notes"},
            "element": {"type": "plain_text_input", "action_id": "value",
                        "multiline": True,
                        "placeholder": {"type": "plain_text", "text": "Interviewer name, focus areas, prep notes…"}},
        },
    ]


@app.command("/cal-add")
def cal_add_command(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "cal_add_submit",
            "title": {"type": "plain_text", "text": "Add Calendar Event"},
            "submit": {"type": "plain_text", "text": "Add"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": _cal_add_blocks(),
        },
    )


def _local_to_utc_iso(date_str: str, time_str: str, tz: str) -> str:
    """Convert a naive date+time in the given IANA timezone to a UTC ISO string."""
    import datetime as _dt
    import zoneinfo as _zi
    try:
        zone = _zi.ZoneInfo(tz)
    except Exception:
        zone = _zi.ZoneInfo("UTC")
    h, m = (time_str.split(":") + ["0"])[:2]
    naive = _dt.datetime(
        int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10]),
        int(h), int(m), 0,
    )
    local_dt = naive.replace(tzinfo=zone)
    utc_dt   = local_dt.astimezone(_dt.timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@app.view("cal_add_submit")
def cal_add_view_submit(ack, body, client, view):
    ack()
    vals     = view["state"]["values"]
    channel  = body["user"]["id"]

    def _txt(block):
        return ((vals.get(block, {}).get("value", {}) or {}).get("value") or "").strip()

    def _sel(block, fallback=""):
        opt = (vals.get(block, {}).get("value", {}) or {}).get("selected_option", {}) or {}
        return opt.get("value") or fallback

    def _date(block):
        return ((vals.get(block, {}).get("value", {}) or {}).get("selected_date") or None)

    def _checks(block):
        opts = ((vals.get(block, {}).get("value", {}) or {}).get("selected_options") or [])
        return [o["value"] for o in opts]

    title      = _txt("title")
    event_type = _sel("event_type", "custom")
    date_str   = _date("event_date")
    time_str   = _txt("event_time") or "09:00"
    user_tz    = _txt("event_tz") or "America/New_York"
    duration   = _txt("duration") or "60"
    app_id     = _sel("app_link", "none")
    offset_str = _txt("reminder_offset")
    channels   = _checks("reminder_channels")
    notes      = _txt("notes")

    if not title or not date_str:
        client.chat_postMessage(channel=channel, text=":x: Title and date are required.")
        return

    # Convert entered local time to UTC using the user's Slack timezone
    time_clean = time_str.replace(".", ":").strip()
    try:
        dt_iso = _local_to_utc_iso(date_str, time_clean, user_tz)
    except Exception:
        client.chat_postMessage(channel=channel, text=":x: Invalid time format. Use HH:MM (e.g. 14:00).")
        return

    reminders = []
    if offset_str and channels:
        try:
            offset_minutes = max(0, int(offset_str))
            reminders = [{"offset_minutes": offset_minutes, "channels": channels}]
        except ValueError:
            pass

    payload = {
        "title":            title,
        "event_type":       event_type,
        "datetime":         dt_iso,
        "timezone":         user_tz,
        "duration_minutes": max(0, min(1440, int(duration) if duration.isdigit() else 60)),
        "notes":            notes,
        "app_id":           app_id if app_id != "none" else None,
        "reminders":        reminders,
    }

    try:
        ev = _create_cal_event(payload)
        type_label = EVENT_TYPE_LABELS.get(event_type, event_type)
        client.chat_postMessage(
            channel=channel,
            text=(
                f":white_check_mark: *{title}* added to calendar\n"
                f"• {type_label}  ·  {_fmt_event_dt(ev.get('datetime',''))}\n"
                f"<{CALENDAR_URL}|Open Calendar →>"
            ),
        )
    except Exception as exc:
        client.chat_postMessage(channel=channel, text=_error_text("Failed to create event", exc))


# ---------------------------------------------------------------------------
# /cal-view — view event details
# ---------------------------------------------------------------------------

@app.command("/cal-view")
def cal_view_command(ack, body, client, respond):
    ack()
    try:
        options = _cal_event_options()
    except Exception as exc:
        respond(_error_text("Could not load calendar", exc))
        return

    if not options:
        respond(f"No events found. Use `/cal-add` to create one or <{CALENDAR_URL}|open the calendar>.")
        return

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "cal_view_submit",
            "title": {"type": "plain_text", "text": "View Event"},
            "submit": {"type": "plain_text", "text": "View"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [{
                "type": "input", "block_id": "event_id",
                "label": {"type": "plain_text", "text": "Select event"},
                "element": {
                    "type": "static_select", "action_id": "value",
                    "placeholder": {"type": "plain_text", "text": "Select an event…"},
                    "options": options,
                },
            }],
        },
    )


@app.view("cal_view_submit")
def cal_view_view_submit(ack, body, client, view):
    ack()
    event_id = view["state"]["values"]["event_id"]["value"]["selected_option"]["value"]
    channel  = body["user"]["id"]
    try:
        r = _api("get", f"/api/calendar/{event_id}")
        r.raise_for_status()
        ev = r.json()
    except Exception as exc:
        client.chat_postMessage(channel=channel, text=_error_text("Could not load event", exc))
        return

    type_label = EVENT_TYPE_LABELS.get(ev.get("event_type", ""), ev.get("event_type", "?"))
    emoji      = EVENT_TYPE_EMOJI.get(ev.get("event_type", ""), "📅")
    lines = [
        f"{emoji} *{ev.get('title')}*",
        f"• Type: {type_label}",
        f"• Time: {_fmt_event_dt(ev.get('datetime',''))} ({ev.get('timezone','UTC')})",
    ]
    if ev.get("duration_minutes"):
        lines.append(f"• Duration: {ev['duration_minutes']} min")
    if ev.get("notes"):
        lines.append(f"• Notes: {ev['notes'][:200]}")
    if ev.get("reminders"):
        for r in ev["reminders"]:
            offset = r.get("offset_minutes", 0)
            label  = f"{offset}m" if offset < 60 else (f"{offset//60}h" if offset < 1440 else f"{offset//1440}d")
            lines.append(f"• 🔔 {label} before via {', '.join(r.get('channels',[]))}")

    lines.append(f"<{CALENDAR_URL}|Open Calendar →>")
    client.chat_postMessage(channel=channel, text="\n".join(lines))


# ---------------------------------------------------------------------------
# /cal-delete — delete a calendar event
# ---------------------------------------------------------------------------

@app.command("/cal-delete")
def cal_delete_command(ack, body, client, respond):
    ack()
    try:
        options = _cal_event_options()
    except Exception as exc:
        respond(_error_text("Could not load calendar", exc))
        return

    if not options:
        respond("No events found.")
        return

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "cal_delete_select",
            "title": {"type": "plain_text", "text": "Delete Event"},
            "submit": {"type": "plain_text", "text": "Continue →"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": ":warning: *This will permanently delete the event and all its reminders.*"},
                },
                {
                    "type": "input", "block_id": "event_id",
                    "label": {"type": "plain_text", "text": "Select event to delete"},
                    "element": {
                        "type": "static_select", "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "Select an event…"},
                        "options": options,
                    },
                },
            ],
        },
    )


@app.view("cal_delete_select")
def cal_delete_select_submit(ack, body, client, view):
    event_id = view["state"]["values"]["event_id"]["value"]["selected_option"]["value"]
    label    = view["state"]["values"]["event_id"]["value"]["selected_option"]["text"]["text"]
    ack({
        "response_action": "push",
        "view": {
            "type": "modal",
            "callback_id": "cal_delete_confirm",
            "title": {"type": "plain_text", "text": "Confirm Delete"},
            "submit": {"type": "plain_text", "text": "Delete"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "private_metadata": event_id,
            "blocks": [{
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":warning: Are you sure you want to delete:\n\n*{label}*\n\nThis cannot be undone.",
                },
            }],
        },
    })


@app.view("cal_delete_confirm")
def cal_delete_confirm_submit(ack, body, client, view):
    ack()
    event_id = view["private_metadata"]
    channel  = body["user"]["id"]
    try:
        _delete_cal_event(event_id)
        client.chat_postMessage(channel=channel, text=":wastebasket: Calendar event deleted.")
    except Exception as exc:
        client.chat_postMessage(channel=channel, text=_error_text("Failed to delete", exc))


# ---------------------------------------------------------------------------
# /help — command reference
# ---------------------------------------------------------------------------

@app.command("/help")
def help_command(ack, respond):
    ack()

    # Fetch current model from API (best-effort)
    model_label = "unknown"
    try:
        r = _api("get", "/api/config/model")
        if r.ok:
            model_label = r.json().get("model", "unknown")
    except Exception:
        pass

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📖  Job Apply — Command Reference"},
        },

        # Agent runs
        {"type": "section", "text": {"type": "mrkdwn", "text": "*🤖  Agent Runs*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            "*/apply* — Generate resume, ATS resume & cover letter for a job\n"
            "*/aq* — Answer an application question using your resume & JD\n"
            "*/prep* — Generate an interview prep document\n"
            "*/thankyou* — Generate a post-interview thank-you email\n"
            "*/optimize* — Refine an existing run's documents from a prompt\n"
            "*/rescore* — Re-score resume/JD match for an application\n"
            "*/runs* — List your recent agent run folders from Drive\n"
            "_apply/prep/aq pick from your tracked applications — add one with_ "
            "*/track-add* _first if you don't have any yet_"
        )}},
        {"type": "divider"},

        # Calendar
        {"type": "section", "text": {"type": "mrkdwn", "text": "*📅  Calendar*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            "*/cal-today* — Show today's events\n"
            "*/cal-week* — Show the next 7 days\n"
            "*/cal-add* — Add a calendar event (with reminders)\n"
            "*/cal-view* — View full details of an event\n"
            "*/cal-delete* — Delete an event (two-step confirm)"
        )}},
        {"type": "divider"},

        # Tracker
        {"type": "section", "text": {"type": "mrkdwn", "text": "*📋  Application Tracker*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            "*/tracker* — Pipeline summary grouped by status\n"
            "*/track-list `[status]`* — List applications (optional: filter by status)\n"
            "*/track-view* — View full details of an application\n"
            "*/track-add* — Add a new application record\n"
            "*/track-update* — Update an application's status (+ optional note)\n"
            "*/track-note* — Add a comment/note to an application\n"
            "*/track-delete* — Delete an application (two-step confirm)"
        )}},
        {"type": "divider"},

        # Lookup
        {"type": "section", "text": {"type": "mrkdwn", "text": "*🔍  Lookup & Info*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            "*/company `[name]`* — Search company info — logo, domain, description\n"
            "*/whoami* — Show your account details and verification status"
        )}},
        {"type": "divider"},

        # Profile
        {"type": "section", "text": {"type": "mrkdwn", "text": "*👤  Profile*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            "*/profile-resume* — Upload a new master resume (.docx)\n"
            "*/profile-guide* — Edit your profile & voice guide\n"
            "*/notifications* — View and toggle email notification preferences"
        )}},
        {"type": "divider"},

        # System
        {"type": "section", "text": {"type": "mrkdwn", "text": "*🛠️  System*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            "*/help* — Show this command reference"
        )}},
        {"type": "divider"},

        # Footer
        {
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": f"Job Apply Agent · <{API_BASE}|Open App> · Model: `{model_label}`"}],
        },
    ]
    respond(blocks=blocks, text="Job Apply — Command Reference")


# ---------------------------------------------------------------------------
# App Home tab
# ---------------------------------------------------------------------------

@app.event("app_home_opened")
def handle_app_home_opened(client, event, logger):
    """Render a dynamic home tab with pipeline stats and command reference."""
    user_id = event["user"]

    # Fetch pipeline data and upcoming calendar events (best-effort)
    try:
        apps = _get_apps()
    except Exception:
        apps = []

    try:
        upcoming = _get_upcoming_events()[:5]
    except Exception:
        upcoming = []

    STATUS_ORDER = [
        "Interviewing", "Phone Screen", "Applied", "On Hold",
        "Researching", "Offer", "Rejected", "Not Applying",
    ]
    STATUS_EMOJI = {
        "Interviewing":  "🎯", "Phone Screen": "📞",
        "Applied":       "✅", "On Hold":       "⏸️",
        "Researching":   "🔬", "Offer":         "🎉",
        "Rejected":      "❌", "Not Applying":  "🚫",
    }

    counts = {s: 0 for s in STATUS_ORDER}
    for a in apps:
        s = a.get("status", "")
        if s in counts:
            counts[s] += 1

    active   = sum(counts[s] for s in ("Interviewing", "Phone Screen", "Applied", "On Hold"))
    pipeline = "\n".join(
        f"{STATUS_EMOJI[s]} *{s}:* {counts[s]}"
        for s in STATUS_ORDER if counts[s]
    ) or "_No applications yet — use `/track-add` to get started._"

    blocks = [
        # Header
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🧑‍💼  Job Apply Agent"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "AI-powered job application agent — tailored resumes, "
                    "cover letters, interview prep, thank-you emails, and application tracking.\n"
                    f"<{API_BASE}|Open web app>  ·  <{API_BASE}/tracking.html|Tracker>  ·  <{API_BASE}/calendar.html|Calendar>"
                ),
            },
        },
        {"type": "divider"},

        # Pipeline summary
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*📊 Your Pipeline* — "
                    f"{len(apps)} total · {active} active\n\n"
                    + pipeline
                ),
            },
        },
        {"type": "divider"},

        # Upcoming calendar events
        *([
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*📅 Upcoming ({len(upcoming)})*\n\n"
                        + "\n".join(_event_line(ev) for ev in upcoming)
                    ),
                },
            },
        ] if upcoming else []),

        # Quick commands
        {"type": "section", "text": {"type": "mrkdwn", "text": "*⚡ Quick Commands*"}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*/apply* — Generate resume + cover letter\n"
                    "*/prep* — Generate interview prep doc\n"
                    "*/thankyou* — Thank-you email\n"
                    "*/cal-today* — Today's events\n"
                    "*/cal-add* — Add calendar event\n"
                    "*/tracker* — Pipeline summary"
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*/track-update* — Update status\n"
                    "*/track-note* — Add a note\n"
                    "*/aq* — Answer application questions\n"
                    "*/runs* — Recent Drive run folders\n"
                    "*/whoami* — Account details\n"
                    "*/help* — Full command reference"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"<{API_BASE}|apply.cdlav.us>  ·  Powered by Claude"},
            ],
        },
    ]

    try:
        client.views_publish(user_id=user_id, view={"type": "home", "blocks": blocks})
    except Exception as exc:
        logger.error(f"Failed to publish home tab: {exc}")


# ---------------------------------------------------------------------------
# /run-tests — run automated test suites and report results
# ---------------------------------------------------------------------------

# Suites available inside the container (no browser — UI tests excluded)
_TEST_SUITES = {
    "unit":  {
        "label": "Unit tests",
        "paths": [
            "tests/test_session.py",
            "tests/test_storage.py",
            "tests/test_webhooks.py",
            "tests/test_apply_utils.py",
        ],
    },
    "api": {
        "label": "API / integration tests",
        "paths": [
            "tests/test_auth.py",
            "tests/test_profile.py",
            "tests/test_health.py",
            "tests/test_runs.py",
            "tests/test_admin.py",
            "tests/test_security_headers.py",
            "tests/test_rate_limiting.py",
        ],
    },
    "slack": {
        "label": "Slack bot tests",
        "paths": ["tests/slack/"],
    },
    "all": {
        "label": "Full suite (unit + API + Slack)",
        "paths": ["tests/", "--ignore=tests/ui"],
    },
    "ui-anon": {
        "label": "UI tests — anonymous (no credentials)",
        "paths": ["tests/ui/test_login.py", "tests/ui/test_register.py"],
        "browser": True,
    },
    "ui": {
        "label": "UI tests — authenticated",
        "paths": ["tests/ui/"],
        "browser": True,
        "needs_creds": True,
    },
    "ui-admin": {
        "label": "UI tests — admin",
        "paths": ["tests/ui/test_admin.py"],
        "browser": True,
        "needs_creds": True,
    },
}

_SUITE_ALIASES = {
    "u": "unit",    "units": "unit",
    "a": "api",     "apis": "api",
    "s": "slack",
    "":  "all",     "full": "all",     "everything": "all",
    "ui": "ui",     "web": "ui",
    "anon": "ui-anon", "ui-anon": "ui-anon",
    "admin": "ui-admin", "ui-admin": "ui-admin",
}

_active_test_run: dict | None = None
_test_run_lock = threading.Lock()


def _resolve_suite(raw: str) -> tuple[str, dict] | tuple[None, None]:
    """Return (suite_key, suite_config) or (None, None) if unknown."""
    key = raw.strip().lower()
    key = _SUITE_ALIASES.get(key, key)
    suite = _TEST_SUITES.get(key)
    return (key, suite) if suite else (None, None)


def _run_pytest(paths: list[str], extra_args: list[str] | None = None,
                env_overrides: dict | None = None) -> dict:
    """Execute pytest in a subprocess and return {passed, failed, errors, output, duration}."""
    cmd = [
        sys.executable, "-m", "pytest",
        "--no-cov", "--tb=short", "-q",
        *paths,
        *(extra_args or []),
    ]
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    t0 = time.time()
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd="/app",
        timeout=300,
        env=env,
    )
    duration = time.time() - t0
    output   = (result.stdout + result.stderr).strip()

    # Parse summary line: "X passed, Y failed, Z error in N.Xs"
    passed = failed = errors = 0
    for line in output.splitlines()[::-1]:
        m = re.search(r"(\d+) passed", line)
        if m:
            passed = int(m.group(1))
        m = re.search(r"(\d+) failed", line)
        if m:
            failed = int(m.group(1))
        m = re.search(r"(\d+) error", line)
        if m:
            errors = int(m.group(1))
        if "passed" in line or "failed" in line or "error" in line:
            break

    return {
        "passed":   passed,
        "failed":   failed,
        "errors":   errors,
        "output":   output,
        "duration": duration,
        "returncode": result.returncode,
    }


def _format_test_results(suite_label: str, r: dict) -> list[dict]:
    """Build Slack blocks summarising the test run."""
    total  = r["passed"] + r["failed"] + r["errors"]
    ok     = r["returncode"] == 0 and r["failed"] == 0 and r["errors"] == 0
    icon   = ":white_check_mark:" if ok else ":x:"
    status = "All tests passed" if ok else f"{r['failed']} failed, {r['errors']} errors"
    dur    = f"{r['duration']:.1f}s"

    header = f"{icon}  *{suite_label}* — {status}  ·  {r['passed']}/{total} passed  ·  _{dur}_"

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
    ]

    if not ok:
        # Extract just the failure sections (lines starting with FAILED or the traceback)
        lines        = r["output"].splitlines()
        failure_lines: list[str] = []
        in_failure   = False
        for line in lines:
            if line.startswith("FAILED") or line.startswith("ERROR "):
                in_failure = True
            if in_failure:
                failure_lines.append(line)
            if in_failure and line == "":
                in_failure = False

        failure_text = "\n".join(failure_lines[:40])  # cap at 40 lines
        if len(failure_text) > 2800:
            failure_text = failure_text[:2800] + "\n…(truncated)"

        if failure_text:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```{failure_text}```"},
            })

    return blocks


@app.command("/run-tests")
def run_tests_command(ack, body, respond, client):
    global _active_test_run

    ack()

    # ── Access control ────────────────────────────────────────────────────
    caller_id = body.get("user_id", "")
    if not TEST_RUNNER_SLACK_USER_ID:
        respond(":lock: Test runner is disabled — `TEST_RUNNER_SLACK_USER_ID` not set.")
        return
    if caller_id != TEST_RUNNER_SLACK_USER_ID:
        respond(":no_entry: You are not authorised to run tests.")
        return

    # ── Resolve suite ────────────────────────────────────────────────────
    raw = body.get("text", "").strip()
    suite_key, suite = _resolve_suite(raw)
    if suite is None:
        keys = ", ".join(f"`{k}`" for k in _TEST_SUITES)
        respond(f":x: Unknown suite `{raw}`. Available: {keys}")
        return

    # ── Concurrency guard ────────────────────────────────────────────────
    with _test_run_lock:
        if _active_test_run is not None:
            respond(f":hourglass: A test run is already in progress (`{_active_test_run['suite']}`). Try again shortly.")
            return
        _active_test_run = {"suite": suite_key}

    # ── Post initial message ─────────────────────────────────────────────
    channel  = body.get("channel_id")
    init_msg = client.chat_postMessage(
        channel=channel,
        text=f":test_tube: Running *{suite['label']}*…",
        blocks=[{
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f":test_tube: Running *{suite['label']}*…  _(this takes ~10–30s)_"},
        }],
    )
    ts = init_msg["ts"]

    # ── Background worker ────────────────────────────────────────────────
    def _worker():
        global _active_test_run
        try:
            extra_args: list[str] = []
            env_overrides: dict[str, str] = {}

            if suite.get("browser"):
                # Headless Chromium — no sandbox needed in container
                extra_args += [
                    "--base-url", os.environ.get("UI_BASE_URL", API_BASE),
                    "--browser", "chromium",
                ]
                # Pass UI test credentials from env (set as Fly secrets)
                for key in ("UI_BASE_URL", "UI_TEST_EMAIL", "UI_TEST_PASSWORD",
                            "UI_ADMIN_EMAIL", "UI_ADMIN_PASSWORD"):
                    val = os.environ.get(key, "")
                    if val:
                        env_overrides[key] = val
                # Default base URL to the live app if not explicitly set
                if "UI_BASE_URL" not in env_overrides:
                    env_overrides["UI_BASE_URL"] = API_BASE

                if suite.get("needs_creds") and not os.environ.get("UI_TEST_PASSWORD"):
                    blocks = [{"type": "section", "text": {"type": "mrkdwn",
                        "text": ":lock: UI test credentials not set. Add `UI_TEST_PASSWORD` and `UI_ADMIN_PASSWORD` as Fly secrets."}}]
                    return

            result = _run_pytest(suite["paths"], extra_args=extra_args,
                                 env_overrides=env_overrides or None)
            blocks = _format_test_results(suite["label"], result)
        except subprocess.TimeoutExpired:
            blocks = [{"type": "section", "text": {"type": "mrkdwn",
                        "text": ":alarm_clock: Test run timed out after 5 minutes."}}]
        except Exception as exc:
            blocks = [{"type": "section", "text": {"type": "mrkdwn",
                        "text": f":x: Test runner crashed: `{exc}`"}}]
        finally:
            with _test_run_lock:
                _active_test_run = None

        try:
            client.chat_update(channel=channel, ts=ts, blocks=blocks,
                               text=blocks[0]["text"]["text"])
        except Exception as exc:
            client.chat_postMessage(channel=channel,
                                    text=_error_text("Could not update result message", exc))

    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Start model auto-upgrade scheduler
    t = threading.Thread(target=_model_check_loop, daemon=True)
    t.start()

    if SLACK_APP_TOKEN:
        handler = SocketModeHandler(app, SLACK_APP_TOKEN)
        print("Starting in Socket Mode")
        handler.start()
    else:
        print(f"Starting HTTP server on port {PORT}")
        app.start(port=PORT)
