"""
Shared fixtures and helpers for the Microsoft Teams bot test suite.

Strategy
--------
teams_bot/ has no __init__.py and uses flat `from config import Config` /
`import api_client` imports (mirroring how it's run standalone via
`python app.py`, and how routers/teams.py mounts it in production) — so we
add teams_bot/ to sys.path once here, exactly like routers/teams.py does,
rather than rewriting it as a package.

Every test runs fully offline — all api_client calls are intercepted by
unittest.mock. Command handlers (_cmd_*, _submit_*) are called directly with
a lightweight mock TurnContext, the same pattern tests/slack/conftest.py uses
for Slack's ack/respond/client mocks.

pytest.ini sets asyncio_mode = auto, so `async def test_...` functions run
with no extra marker needed.
"""

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("MICROSOFT_APP_ID", "test-app-id")
os.environ.setdefault("MICROSOFT_APP_PASSWORD", "test-app-password")
os.environ.setdefault("BOT_API_KEY", "test-bot-api-key")
os.environ.setdefault("JOB_APPLY_API_URL", "https://test.example.com")

_TEAMS_BOT_DIR = str(Path(__file__).resolve().parent.parent.parent / "teams_bot")
if _TEAMS_BOT_DIR not in sys.path:
    sys.path.insert(0, _TEAMS_BOT_DIR)


@pytest.fixture(scope="session")
def bot_module():
    """Import teams_bot/bot.py once per session (flat top-level import, same
    sys.path trick routers/teams.py uses in production)."""
    import bot as _bot
    return _bot


@pytest.fixture
def bot(bot_module):
    """A fresh JobApplyBot() per test — the class holds no instance state,
    so this just avoids any risk of cross-test leakage."""
    return bot_module.JobApplyBot()


# ── Activity / TurnContext builders ────────────────────────────────────────

def make_activity(
    text: str = "",
    value: dict | None = None,
    attachments: list | None = None,
    entities: list | None = None,
    name: str | None = None,
    from_id: str = "user-1",
    aad_object_id: str | None = "aad-obj-1",
):
    """A real botbuilder Activity — needed because
    TurnContext.get_conversation_reference() (called directly by the
    background-run submit handlers) reads real Activity fields, not mock
    attributes."""
    from botbuilder.schema import Activity, ActivityTypes, ChannelAccount, ConversationAccount
    return Activity(
        type=ActivityTypes.message,
        id="activity-1",
        name=name,
        text=text,
        value=value,
        attachments=attachments or [],
        entities=entities or [],
        from_property=ChannelAccount(id=from_id, aad_object_id=aad_object_id, name="Test User"),
        recipient=ChannelAccount(id="bot-1", name="Landed Bot"),
        conversation=ConversationAccount(id="conv-1"),
        channel_id="msteams",
        service_url="https://smba.trafficmanager.net/amer/",
        locale="en-US",
    )


def make_ctx(**kwargs) -> MagicMock:
    """A mock TurnContext: a real Activity on .activity, an AsyncMock
    send_activity, and a MagicMock adapter for the proactive-messaging path."""
    ctx = MagicMock()
    ctx.activity = make_activity(**kwargs)
    ctx.send_activity = AsyncMock()
    ctx.adapter = MagicMock()
    return ctx


def make_entity_mention(text: str = "<at>Landed</at>"):
    from botbuilder.schema import Entity
    e = Entity(type="mention")
    e.additional_properties = {"text": text}
    return e


def make_file_attachment(name: str = "resume.docx", download_url: str = "https://contoso.sharepoint.com/resume.docx"):
    from botbuilder.schema import Attachment
    return Attachment(
        content_type="application/vnd.microsoft.teams.file.download.info",
        name=name,
        content={"downloadUrl": download_url, "uniqueId": "file-1", "fileType": "docx"},
    )


def sent_texts(ctx: MagicMock) -> list[str]:
    """All plain-text bodies sent via ctx.send_activity across every call,
    for asserting on message content without caring about call ordering."""
    out = []
    for c in ctx.send_activity.call_args_list:
        activity = c.args[0]
        if getattr(activity, "text", None):
            out.append(activity.text)
    return out


def sent_cards(ctx: MagicMock) -> list[dict]:
    """All Adaptive Card dicts attached via ctx.send_activity across every call."""
    out = []
    for c in ctx.send_activity.call_args_list:
        activity = c.args[0]
        for att in getattr(activity, "attachments", None) or []:
            if att.content_type == "application/vnd.microsoft.card.adaptive":
                out.append(att.content)
    return out


class SyncThread:
    """Drop-in replacement for threading.Thread that runs target()
    synchronously on .start(). The background-run submit handlers
    (_submit_apply, _submit_optimize, etc.) fire a real daemon thread in
    production; tests patch `bot.threading.Thread` with this so the thread
    body runs inline and assertions can run immediately after."""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def fake_response(status_code: int = 200, json_data: Any = None) -> MagicMock:
    """Mock requests.Response — same convention as tests/slack/conftest.py."""
    r = MagicMock()
    r.status_code = status_code
    r.ok = status_code < 400
    r.json.return_value = json_data if json_data is not None else {}
    if status_code >= 400:
        import requests
        r.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status_code}", response=r)
    else:
        r.raise_for_status.return_value = None
    return r


# ── Sample data ─────────────────────────────────────────────────────────────

SAMPLE_APPS = [
    {
        "id": "app-001",
        "company": "Salesforce",
        "role_title": "Senior Engineer",
        "status": "Interviewing",
        "date_applied": "2026-05-01T00:00:00Z",
        "url": "https://salesforce.com/jobs/1",
        "domain": "salesforce.com",
        "recruiter_name": "Jane Smith",
        "linked_runs": [
            {"gdrive_folder_id": "folder-1", "folder_url": "https://drive.google.com/folder-1",
             "linked_at": "2026-05-02T00:00:00Z", "type": "resume"},
        ],
    },
    {
        "id": "app-002",
        "company": "Stripe",
        "role_title": "Staff Engineer",
        "status": "Applied",
        "date_applied": "2026-05-10T00:00:00Z",
        "url": "https://stripe.com/jobs/2",
        "domain": "stripe.com",
        "recruiter_name": "",
        "linked_runs": [],
    },
    {
        "id": "app-003",
        "company": "Figma",
        "role_title": "Backend Engineer",
        "status": "Rejected",
        "date_applied": "2026-04-15T00:00:00Z",
        "url": "",
        "domain": "figma.com",
        "linked_runs": [],
    },
]

SAMPLE_APP = SAMPLE_APPS[0]

SAMPLE_LINK_STATUS_LINKED = {"linked": True, "email": "test@example.com", "expires_at": 1999999999}
SAMPLE_LINK_STATUS_UNLINKED = {"linked": False}

SAMPLE_PROFILE = {
    "email": "test@example.com",
    "display_name": "Test User",
    "role": "user",
    "email_verified": True,
    "has_resume": True,
    "resume_filename": "master.docx",
    "profile_text": "Direct, no corporate filler.",
    "notification_prefs": {"daily_digest": True, "weekly_digest": False},
}

SAMPLE_EVENT = {
    "id": "event-1",
    "title": "HM Interview — Salesforce",
    "event_type": "interview",
    "datetime": "2026-07-10T14:00:00Z",
    "timezone": "America/New_York",
    "duration_minutes": 60,
    "notes": "Focus on platform scalability.",
    "reminders": [{"offset_minutes": 1440, "channels": ["email"]}],
}
