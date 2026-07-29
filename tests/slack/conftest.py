"""
Shared fixtures and helpers for the Slack bot test suite.

Strategy
--------
slack_bot.py reads os.environ at import time, so we patch those vars before
the module is imported.  Every test runs fully offline — all _api() calls and
requests.get() calls are intercepted by pytest-mock / responses.

The handler functions are called directly:

    tracker_command(ack=mock_ack, respond=mock_respond)

This is the same pattern used by Slack's own Bolt test utilities.
"""

import os
import sys
import importlib
import json
from unittest.mock import MagicMock, call
from typing import Any

import pytest

# ── Stub env vars BEFORE slack_bot is imported ─────────────────────────────

os.environ.setdefault("SLACK_BOT_TOKEN",      "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")
os.environ.setdefault("BOT_API_KEY",          "test-bot-api-key")
os.environ.setdefault("JOB_APPLY_API_URL",    "https://test.example.com")
os.environ.setdefault("ANTHROPIC_API_KEY",    "sk-ant-test")


class _PassthroughApp:
    """
    Stub for slack_bolt.App that lets all decorators pass through unchanged.

    When slack_bot.py does @app.command("/foo"), the real App stores the handler
    and returns it untouched.  A MagicMock would replace the function with
    another MagicMock — making it impossible to call the real handler in tests.

    This class acts as an identity decorator for every Slack event type so that
    slack_bot module-level names (apply_command, tracker_command, etc.) stay
    as the real Python functions.
    """
    def __init__(self, *args, **kwargs):
        pass  # accept token=, signing_secret=, etc.

    def _identity_decorator(self, *args, **kwargs):
        def decorator(fn):
            return fn
        return decorator

    command    = _identity_decorator
    view       = _identity_decorator
    options    = _identity_decorator
    event      = _identity_decorator
    action     = _identity_decorator
    message    = _identity_decorator
    shortcut   = _identity_decorator
    middleware = _identity_decorator


# ── Lazy import — prevents re-importing on every test ─────────────────────

@pytest.fixture(scope="session")
def bot(monkeypatch_session):
    """Import slack_bot once per session with a mocked Slack App constructor."""
    # Patch slack_bolt.App so it doesn't try to validate the token
    import slack_bolt
    monkeypatch_session.setattr(slack_bolt, "App", MagicMock(return_value=MagicMock()))
    import slack_bot as _bot
    importlib.reload(_bot)
    return _bot


@pytest.fixture(scope="session")
def monkeypatch_session():
    """Session-scoped monkeypatch for the bot import."""
    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    yield mp
    mp.undo()


# ── Convenience mock factory ───────────────────────────────────────────────

def make_ack() -> MagicMock:
    """Return a fresh ack() mock."""
    return MagicMock()


def make_respond() -> MagicMock:
    """Return a fresh respond() mock that records all calls."""
    return MagicMock()


def make_client() -> MagicMock:
    """Return a Slack WebClient-like mock."""
    client = MagicMock()
    client.views_open.return_value = {"ok": True}
    client.views_push.return_value = {"ok": True}
    client.chat_postMessage.return_value = {"ok": True}
    return client


def make_body(
    user_id: str = "U123TEST",
    user_name: str = "testuser",
    channel_id: str = "C123TEST",
    text: str = "",
    trigger_id: str = "trigger.123",
    team_id: str = "T123TEST",
) -> dict[str, Any]:
    """Return a minimal Slack slash-command body dict."""
    return {
        "user_id":    user_id,
        "user_name":  user_name,
        "channel_id": channel_id,
        "text":       text,
        "trigger_id": trigger_id,
        "team_id":    team_id,
    }


def fake_response(status_code: int = 200, json_data: Any = None) -> MagicMock:
    """Return a mock requests.Response."""
    r = MagicMock()
    r.status_code = status_code
    r.ok = (status_code < 400)
    r.json.return_value = json_data if json_data is not None else {}
    if status_code >= 400:
        import requests
        r.raise_for_status.side_effect = requests.HTTPError(
            f"HTTP {status_code}", response=r
        )
    else:
        r.raise_for_status.return_value = None
    return r


# ── Sample data fixtures ───────────────────────────────────────────────────

SAMPLE_APPS = [
    {
        "id": "app-001",
        "company": "Salesforce",
        "role_title": "Senior Engineer",
        "status": "Interviewing",
        "date_applied": "2026-05-01T00:00:00Z",
        "url": "https://salesforce.com/jobs/1",
        "priority": "High",
        "recruiter_name": "Jane Smith",
        "domain": "salesforce.com",
    },
    {
        "id": "app-002",
        "company": "Stripe",
        "role_title": "Staff Engineer",
        "status": "Applied",
        "date_applied": "2026-05-10T00:00:00Z",
        "url": "https://stripe.com/jobs/2",
        "priority": "High",
        "recruiter_name": "",
        "domain": "stripe.com",
    },
    {
        "id": "app-003",
        "company": "Figma",
        "role_title": "Backend Engineer",
        "status": "Rejected",
        "date_applied": "2026-04-15T00:00:00Z",
        "url": "",
        "priority": "Medium",
        "recruiter_name": "",
        "domain": "figma.com",
    },
    {
        "id": "app-004",
        "company": "Linear",
        "role_title": "Engineer",
        "status": "Researching",
        "date_applied": "",
        "url": "https://linear.app/jobs",
        "priority": "Low",
        "recruiter_name": "",
        "domain": "linear.app",
    },
]

SAMPLE_ME = {
    "user_id": "uid-test",
    "email": "test@example.com",
    "display_name": "Test User",
    "role": "user",
    "email_verified": True,
    "has_resume": True,
    "has_profile": True,
}

SAMPLE_HEALTH = {
    "status": "ok",
    "storage": "ok",
    "email": "ok",
    "gdrive": "configured",
    "anthropic": "ok",
    "model": "claude-sonnet-4-5",
    "fly_machine": "abc123",
    "fly_app": "job-apply-corey",
}
