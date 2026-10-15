"""Isolation tests for the chatbot's read-only user-data tools."""

import json
from unittest.mock import patch

from scripts import chat_tools


def _run(name, args=None, user_id="user-a", email="a@example.com"):
    return json.loads(chat_tools.run_tool(name, args, user_id, email))


def test_no_tool_schema_accepts_user_identity():
    for tool in chat_tools.CHAT_TOOLS:
        props = tool["input_schema"].get("properties", {})
        assert not {"user_id", "email", "user"} & set(props), tool["name"]


def test_anonymous_gets_nothing():
    assert _run("list_my_applications", user_id="anonymous") == {"error": "not authenticated"}
    assert _run("list_my_applications", user_id="") == {"error": "not authenticated"}


def test_lookups_are_scoped_to_session_user():
    seen = []

    def fake_list(user_id, status=None, **_):
        seen.append(user_id)
        return {"items": [{"id": "1", "company": user_id}]}

    with patch.object(chat_tools.app_store, "list_applications", fake_list):
        out = _run("list_my_applications", {"user_id": "user-b"}, user_id="user-a")
    assert seen == ["user-a"]
    assert out["applications"][0]["company"] == "user-a"


def test_foreign_application_id_not_found():
    with patch.object(chat_tools.app_store, "get_application", lambda uid, aid: None):
        assert _run("get_my_application", {"app_id": "someone-elses"}) == {"error": "not found"}


def test_invalid_ids_rejected_before_lookup():
    assert _run("get_my_application", {"app_id": "../users/b"}) == {"error": "invalid app_id"}
    assert _run("get_my_agent_run", {"run_id": "a/b"}) == {"error": "invalid run_id"}
    assert _run("get_my_drive_job_posting", {"folder_id": "x y"}) == {"error": "invalid folder_id"}


def test_drive_folder_must_belong_to_user():
    with patch.object(chat_tools, "_linked_folder_ids", lambda uid: {"mine"}), \
         patch.object(chat_tools, "_user_drive_folders", lambda uid, em: [{"id": "also-mine"}]):
        assert _run("get_my_drive_job_posting", {"folder_id": "theirs"}) == {"error": "not found"}


def test_audit_log_dropped_and_long_text_clipped():
    record = {"id": "1", "audit_log": [{"x": 1}], "notes": "n" * 20000}
    with patch.object(chat_tools.app_store, "get_application", lambda uid, aid: record):
        out = _run("get_my_application", {"app_id": "1"})
    assert "audit_log" not in out
    assert len(out["notes"]) < 7000


def test_tool_exception_is_contained():
    def boom(*a, **k):
        raise RuntimeError("s3 down")
    with patch.object(chat_tools.run_store, "list_for_user", boom):
        assert "error" in _run("list_my_agent_runs")


def test_calendar_defaults_to_upcoming_only():
    got = {}

    def fake(uid, from_dt=None, to_dt=None):
        got["from"] = from_dt
        return []

    with patch.object(chat_tools.cal_store, "list_events", fake):
        _run("list_my_calendar_events")
        assert got["from"] and got["from"].endswith("Z")
        _run("list_my_calendar_events", {"from": "1970-01-01T00:00:00Z"})
        assert got["from"] == "1970-01-01T00:00:00Z"
