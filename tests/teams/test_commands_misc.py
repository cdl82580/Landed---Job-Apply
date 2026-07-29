"""
Tests for miscellaneous Teams bot commands: calendar (_send_event_list,
_cmd_cal_today/_cmd_cal_week/_cmd_cal_add/_cmd_cal_view/_cmd_cal_delete),
_cmd_company, _cmd_profile_resume, _handle_file_upload, _cmd_profile_guide,
_cmd_notifications, _cmd_runs, and _cmd_help.
"""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.teams.conftest import (
    make_ctx, make_file_attachment, sent_texts, sent_cards, SAMPLE_APPS, SAMPLE_EVENT, SAMPLE_PROFILE,
)


def _mock_download_response(content: bytes, content_length: str | None = None) -> MagicMock:
    """Mimic requests.Response as used by bot._download_capped: usable as a
    context manager, with .headers.get(...), .iter_content(), and
    .raise_for_status() all behaving like the real thing."""
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.raise_for_status.return_value = None
    resp.headers = {"Content-Length": content_length} if content_length is not None else {}
    resp.iter_content.return_value = [content] if content else []
    resp.content = content
    return resp


# ── _send_event_list ──────────────────────────────────────────────────────

class TestSendEventList:
    async def test_renders_header_and_event_rows(self, bot):
        ctx = make_ctx()
        events = [
            {**SAMPLE_EVENT, "id": "e1", "title": "Phone Screen — Stripe", "event_type": "phone_screen"},
            {**SAMPLE_EVENT, "id": "e2", "title": "Onsite — Figma", "event_type": "interview"},
        ]
        await bot._send_event_list(ctx, "My Header", events)
        card = sent_cards(ctx)[0]
        assert card["body"][0] == {"type": "TextBlock", "text": "My Header", "size": "Large", "weight": "Bolder", "wrap": True}
        rows = card["body"][1:]
        assert len(rows) == 2
        for row in rows:
            assert row["type"] == "ColumnSet"

    async def test_caps_at_limit_and_adds_more_footer(self, bot):
        ctx = make_ctx()
        events = [{**SAMPLE_EVENT, "id": f"e{i}", "title": f"Event {i}"} for i in range(5)]
        await bot._send_event_list(ctx, "Header", events, limit=3)
        card = sent_cards(ctx)[0]
        rows = card["body"][1:4]
        assert len(rows) == 3
        footer = card["body"][4]
        assert footer["text"] == "…and 2 more."

    async def test_no_footer_when_under_limit(self, bot):
        ctx = make_ctx()
        events = [SAMPLE_EVENT]
        await bot._send_event_list(ctx, "Header", events, limit=20)
        card = sent_cards(ctx)[0]
        assert len(card["body"]) == 2


# ── _cmd_cal_today ────────────────────────────────────────────────────────

class TestCmdCalToday:
    async def test_error_sends_error_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_calendar_events", side_effect=Exception("boom")):
            await bot._cmd_cal_today(ctx, {"email": "a@b.com"})
        assert "Could not load calendar" in sent_texts(ctx)[0]

    async def test_no_events_sends_empty_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_calendar_events", return_value=[]):
            await bot._cmd_cal_today(ctx, {"email": "a@b.com"})
        assert "No events today" in sent_texts(ctx)[0]

    async def test_events_found_calls_send_event_list_with_count_header(self, bot):
        ctx = make_ctx()
        events = [SAMPLE_EVENT, {**SAMPLE_EVENT, "id": "e2"}]
        with patch("api_client.get_calendar_events", return_value=events), \
             patch.object(type(bot), "_send_event_list", new=AsyncMock()) as mock_send:
            await bot._cmd_cal_today(ctx, {"email": "a@b.com"})
        mock_send.assert_awaited_once()
        args = mock_send.call_args.args
        assert args[0] is ctx
        assert "2 events" in args[1]
        assert args[2] == events

    async def test_passes_today_utc_day_boundary_window(self, bot):
        ctx = make_ctx()
        today = date.today()
        with patch("api_client.get_calendar_events", return_value=[]) as mock_get:
            await bot._cmd_cal_today(ctx, {"email": "a@b.com"})
        mock_get.assert_called_once_with(f"{today}T00:00:00Z", f"{today}T23:59:59Z", user_email="a@b.com")


# ── _cmd_cal_week ─────────────────────────────────────────────────────────

class TestCmdCalWeek:
    async def test_error_sends_error_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_upcoming_events", side_effect=Exception("boom")):
            await bot._cmd_cal_week(ctx, {"email": "a@b.com"})
        assert "Could not load calendar" in sent_texts(ctx)[0]

    async def test_no_events_sends_empty_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_upcoming_events", return_value=[]):
            await bot._cmd_cal_week(ctx, {"email": "a@b.com"})
        assert "No events in the next 7 days" in sent_texts(ctx)[0]

    async def test_events_found_calls_send_event_list_with_count_header(self, bot):
        ctx = make_ctx()
        events = [SAMPLE_EVENT]
        with patch("api_client.get_upcoming_events", return_value=events), \
             patch.object(type(bot), "_send_event_list", new=AsyncMock()) as mock_send:
            await bot._cmd_cal_week(ctx, {"email": "a@b.com"})
        mock_send.assert_awaited_once()
        args = mock_send.call_args.args
        assert "1 event" in args[1]
        assert args[2] == events

    async def test_get_upcoming_events_called_with_user_email(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_upcoming_events", return_value=[]) as mock_get:
            await bot._cmd_cal_week(ctx, {"email": "a@b.com"})
        mock_get.assert_called_once_with(user_email="a@b.com")


# ── _cmd_cal_add ──────────────────────────────────────────────────────────

class TestCmdCalAdd:
    async def test_sends_form_card_with_app_link_choices(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=SAMPLE_APPS):
            await bot._cmd_cal_add(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        app_link = next(f for f in card["body"] if f.get("id") == "app_link")
        assert app_link["choices"][0] == {"title": "— None —", "value": "none"}
        assert len(app_link["choices"]) == 1 + len(SAMPLE_APPS)
        assert app_link["choices"][1]["value"] == SAMPLE_APPS[0]["id"]

    async def test_get_applications_error_falls_back_to_none_only(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", side_effect=Exception("down")):
            await bot._cmd_cal_add(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        app_link = next(f for f in card["body"] if f.get("id") == "app_link")
        assert app_link["choices"] == [{"title": "— None —", "value": "none"}]

    async def test_form_has_title_input(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_applications", return_value=[]):
            await bot._cmd_cal_add(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        title_field = next(f for f in card["body"] if f.get("id") == "title")
        assert title_field["type"] == "Input.Text"
        assert title_field["isRequired"] is True


# ── _cmd_cal_view ─────────────────────────────────────────────────────────

class TestCmdCalView:
    async def test_error_sends_error_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_calendar_events", side_effect=Exception("boom")):
            await bot._cmd_cal_view(ctx, {"email": "a@b.com"})
        assert "Could not load calendar" in sent_texts(ctx)[0]

    async def test_no_events_sends_empty_state_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_calendar_events", return_value=[]):
            await bot._cmd_cal_view(ctx, {"email": "a@b.com"})
        assert "No events found" in sent_texts(ctx)[0]

    async def test_events_found_sends_sorted_choiceset(self, bot):
        ctx = make_ctx()
        events = [
            {**SAMPLE_EVENT, "id": "later", "datetime": "2026-08-01T00:00:00Z", "title": "Later"},
            {**SAMPLE_EVENT, "id": "sooner", "datetime": "2026-07-01T00:00:00Z", "title": "Sooner"},
        ]
        with patch("api_client.get_calendar_events", return_value=events):
            await bot._cmd_cal_view(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        choice_field = next(f for f in card["body"] if f.get("id") == "event_id")
        assert [c["value"] for c in choice_field["choices"]] == ["sooner", "later"]
        submit_action = next(a for a in card["actions"] if a["type"] == "Action.Submit")
        assert submit_action["data"]["action"] == "cal_view_submit"


# ── _cmd_cal_delete ───────────────────────────────────────────────────────

class TestCmdCalDelete:
    async def test_error_sends_error_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_calendar_events", side_effect=Exception("boom")):
            await bot._cmd_cal_delete(ctx, {"email": "a@b.com"})
        assert "Could not load calendar" in sent_texts(ctx)[0]

    async def test_no_events_sends_empty_state_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_calendar_events", return_value=[]):
            await bot._cmd_cal_delete(ctx, {"email": "a@b.com"})
        assert "No events found" in sent_texts(ctx)[0]

    async def test_events_found_sends_sorted_choiceset(self, bot):
        ctx = make_ctx()
        events = [
            {**SAMPLE_EVENT, "id": "later", "datetime": "2026-08-01T00:00:00Z", "title": "Later"},
            {**SAMPLE_EVENT, "id": "sooner", "datetime": "2026-07-01T00:00:00Z", "title": "Sooner"},
        ]
        with patch("api_client.get_calendar_events", return_value=events):
            await bot._cmd_cal_delete(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        choice_field = next(f for f in card["body"] if f.get("id") == "event_id")
        assert [c["value"] for c in choice_field["choices"]] == ["sooner", "later"]
        submit_action = next(a for a in card["actions"] if a["type"] == "Action.Submit")
        assert submit_action["data"]["action"] == "cal_delete_select_submit"


# ── _cmd_company ──────────────────────────────────────────────────────────

class TestCmdCompany:
    async def test_empty_query_sends_usage(self, bot):
        ctx = make_ctx()
        await bot._cmd_company(ctx, "")
        assert "Usage: **company" in sent_texts(ctx)[0]

    async def test_whitespace_query_sends_usage(self, bot):
        ctx = make_ctx()
        await bot._cmd_company(ctx, "   ")
        assert "Usage: **company" in sent_texts(ctx)[0]

    async def test_search_error_sends_error_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.search_companies", side_effect=Exception("boom")):
            await bot._cmd_company(ctx, "Salesforce")
        assert "Search failed" in sent_texts(ctx)[0]

    async def test_no_results_sends_no_results_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.search_companies", return_value=[]):
            await bot._cmd_company(ctx, "Salesforce")
        assert "No results found" in sent_texts(ctx)[0]

    async def test_results_found_sends_card_with_rows_capped_at_five(self, bot):
        ctx = make_ctx()
        results = [{"name": f"Company {i}", "domain": f"c{i}.com"} for i in range(8)]
        with patch("api_client.search_companies", return_value=results):
            await bot._cmd_company(ctx, "co")
        card = sent_cards(ctx)[0]
        rows = card["body"][1:]
        assert len(rows) == 5

    async def test_result_with_domain_includes_logo_column(self, bot):
        ctx = make_ctx()
        results = [{"name": "Salesforce", "domain": "salesforce.com", "description": "CRM"}]
        with patch("api_client.search_companies", return_value=results):
            await bot._cmd_company(ctx, "salesforce")
        card = sent_cards(ctx)[0]
        row = card["body"][1]
        assert row["columns"][0]["type"] == "Column"
        assert row["columns"][0]["items"][0]["type"] == "Image"

    async def test_result_without_domain_has_no_logo_column(self, bot):
        ctx = make_ctx()
        results = [{"name": "NoDomainCo", "domain": "", "description": "desc"}]
        with patch("api_client.search_companies", return_value=results):
            await bot._cmd_company(ctx, "nodomain")
        card = sent_cards(ctx)[0]
        row = card["body"][1]
        assert len(row["columns"]) == 1
        assert row["columns"][0]["items"][0]["type"] == "TextBlock"


# ── _cmd_profile_resume ───────────────────────────────────────────────────

class TestCmdProfileResume:
    async def test_sends_static_instructions_no_api_calls(self, bot):
        ctx = make_ctx()
        await bot._cmd_profile_resume(ctx)
        text = sent_texts(ctx)[0]
        assert "Upload your master resume" in text
        assert ".docx" in text


# ── _handle_file_upload ───────────────────────────────────────────────────

class TestHandleFileUpload:
    async def test_no_matching_attachment_returns_false(self, bot):
        ctx = make_ctx(attachments=[])
        result = await bot._handle_file_upload(ctx, {"email": "a@b.com"})
        assert result is False
        ctx.send_activity.assert_not_awaited()

    async def test_non_docx_attachment_returns_false(self, bot):
        att = make_file_attachment(name="resume.pdf")
        ctx = make_ctx(attachments=[att])
        result = await bot._handle_file_upload(ctx, {"email": "a@b.com"})
        assert result is False
        ctx.send_activity.assert_not_awaited()

    async def test_deserialize_failure_sends_error_and_returns_true(self, bot):
        att = make_file_attachment()
        att.content = "not-a-dict"
        ctx = make_ctx(attachments=[att])
        result = await bot._handle_file_upload(ctx, {"email": "a@b.com"})
        assert result is True
        assert "Could not read the uploaded file" in sent_texts(ctx)[0]

    async def test_missing_download_url_sends_error_and_returns_true(self, bot):
        att = make_file_attachment()
        att.content = {"uniqueId": "file-1", "fileType": "docx"}
        ctx = make_ctx(attachments=[att])
        result = await bot._handle_file_upload(ctx, {"email": "a@b.com"})
        assert result is True
        assert "Could not read the uploaded file" in sent_texts(ctx)[0]

    async def test_untrusted_download_host_rejected(self, bot):
        att = make_file_attachment(download_url="https://evil.example.com/resume.docx")
        ctx = make_ctx(attachments=[att])
        with patch("requests.get") as mock_get:
            result = await bot._handle_file_upload(ctx, {"email": "a@b.com"})
        mock_get.assert_not_called()
        assert result is True
        assert "Could not read the uploaded file" in sent_texts(ctx)[0]

    async def test_download_failure_sends_error_and_returns_true(self, bot):
        att = make_file_attachment()
        ctx = make_ctx(attachments=[att])
        with patch("requests.get", side_effect=Exception("network down")):
            result = await bot._handle_file_upload(ctx, {"email": "a@b.com"})
        assert result is True
        assert "Could not download the file" in sent_texts(ctx)[0]

    async def test_non_zip_bytes_sends_invalid_docx_error_and_returns_true(self, bot):
        att = make_file_attachment()
        ctx = make_ctx(attachments=[att])
        bad_resp = _mock_download_response(b"not a zip file")
        with patch("requests.get", return_value=bad_resp):
            result = await bot._handle_file_upload(ctx, {"email": "a@b.com"})
        assert result is True
        assert "doesn't look like a valid .docx" in sent_texts(ctx)[0]

    async def test_success_uploads_resume_and_returns_true(self, bot):
        att = make_file_attachment(name="master.docx")
        ctx = make_ctx(attachments=[att])
        content = b"PK\x03\x04rest-of-zip-bytes"
        good_resp = _mock_download_response(content)
        with patch("requests.get", return_value=good_resp), \
             patch("api_client.upload_resume", return_value=None) as mock_upload:
            result = await bot._handle_file_upload(ctx, {"email": "a@b.com"})
        assert result is True
        mock_upload.assert_called_once_with("master.docx", content, user_email="a@b.com")
        assert "master.docx" in sent_texts(ctx)[0]
        assert "saved as your master resume" in sent_texts(ctx)[0]

    async def test_upload_resume_error_sends_error_and_returns_true(self, bot):
        att = make_file_attachment(name="master.docx")
        ctx = make_ctx(attachments=[att])
        good_resp = _mock_download_response(b"PK\x03\x04rest-of-zip-bytes")
        with patch("requests.get", return_value=good_resp), \
             patch("api_client.upload_resume", side_effect=Exception("save failed")):
            result = await bot._handle_file_upload(ctx, {"email": "a@b.com"})
        assert result is True
        assert "Failed to save resume" in sent_texts(ctx)[0]

    async def test_oversized_content_length_rejected_before_download(self, bot):
        """Content-Length alone should reject the file — iter_content must
        never even be consulted."""
        att = make_file_attachment(name="master.docx")
        ctx = make_ctx(attachments=[att])
        oversized_resp = _mock_download_response(
            b"", content_length=str(11 * 1024 * 1024),
        )
        with patch("requests.get", return_value=oversized_resp), \
             patch("api_client.upload_resume") as mock_upload:
            result = await bot._handle_file_upload(ctx, {"email": "a@b.com"})
        assert result is True
        assert "too large" in sent_texts(ctx)[0].lower()
        oversized_resp.iter_content.assert_not_called()
        mock_upload.assert_not_called()

    async def test_oversized_streamed_body_rejected_without_content_length(self, bot):
        """A missing/understated Content-Length must not bypass the cap —
        the streamed byte count itself has to enforce it."""
        att = make_file_attachment(name="master.docx")
        ctx = make_ctx(attachments=[att])
        oversized_chunk = b"P" * (11 * 1024 * 1024)
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        resp.raise_for_status.return_value = None
        resp.headers = {}  # no Content-Length at all
        resp.iter_content.return_value = [oversized_chunk]
        with patch("requests.get", return_value=resp), \
             patch("api_client.upload_resume") as mock_upload:
            result = await bot._handle_file_upload(ctx, {"email": "a@b.com"})
        assert result is True
        assert "too large" in sent_texts(ctx)[0].lower()
        mock_upload.assert_not_called()


# ── _cmd_profile_guide ────────────────────────────────────────────────────

class TestCmdProfileGuide:
    async def test_prefills_guide_from_profile_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_profile", return_value=SAMPLE_PROFILE):
            await bot._cmd_profile_guide(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        guide_field = next(f for f in card["body"] if f.get("id") == "guide")
        assert guide_field["value"] == SAMPLE_PROFILE["profile_text"]

    async def test_get_profile_error_renders_empty_default(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_profile", side_effect=Exception("down")):
            await bot._cmd_profile_guide(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        guide_field = next(f for f in card["body"] if f.get("id") == "guide")
        assert guide_field["value"] == ""


# ── _cmd_notifications ────────────────────────────────────────────────────

class TestCmdNotifications:
    async def test_value_is_comma_joined_enabled_prefs(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_profile", return_value=SAMPLE_PROFILE):
            await bot._cmd_notifications(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        prefs_field = next(f for f in card["body"] if f.get("id") == "prefs")
        enabled = prefs_field["value"].split(",")
        assert "daily_digest" in enabled
        assert "weekly_digest" not in enabled

    async def test_get_profile_error_defaults_all_enabled(self, bot, bot_module):
        ctx = make_ctx()
        with patch("api_client.get_profile", side_effect=Exception("down")):
            await bot._cmd_notifications(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        prefs_field = next(f for f in card["body"] if f.get("id") == "prefs")
        enabled = set(prefs_field["value"].split(","))
        assert enabled == set(bot_module._NOTIF_LABELS.keys())

    async def test_missing_notification_prefs_key_defaults_all_enabled(self, bot, bot_module):
        ctx = make_ctx()
        profile = {**SAMPLE_PROFILE}
        profile.pop("notification_prefs", None)
        with patch("api_client.get_profile", return_value=profile):
            await bot._cmd_notifications(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        prefs_field = next(f for f in card["body"] if f.get("id") == "prefs")
        assert set(prefs_field["value"].split(",")) == set(bot_module._NOTIF_LABELS.keys())

    async def test_choices_use_notif_labels(self, bot, bot_module):
        ctx = make_ctx()
        with patch("api_client.get_profile", return_value=SAMPLE_PROFILE):
            await bot._cmd_notifications(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        prefs_field = next(f for f in card["body"] if f.get("id") == "prefs")
        assert len(prefs_field["choices"]) == len(bot_module._NOTIF_LABELS)
        assert prefs_field["isMultiSelect"] is True


# ── _cmd_runs ──────────────────────────────────────────────────────────────

class TestCmdRuns:
    async def test_error_sends_error_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_agent_runs", side_effect=Exception("boom")):
            await bot._cmd_runs(ctx, {"email": "a@b.com"})
        assert "Error" in sent_texts(ctx)[0]

    async def test_no_runs_sends_empty_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_agent_runs", return_value=[]):
            await bot._cmd_runs(ctx, {"email": "a@b.com"})
        assert "No agent runs found" in sent_texts(ctx)[0]

    async def test_runs_found_sends_card_with_rows(self, bot):
        ctx = make_ctx()
        runs = [
            {"id": "run-1", "type": "resume", "status": "completed", "company": "Salesforce",
             "role": "SE", "app_id": "app-001", "gdrive_folder_url": "https://drive/1"},
        ]
        with patch("api_client.get_agent_runs", return_value=runs), \
             patch("api_client.get_applications", return_value=SAMPLE_APPS):
            await bot._cmd_runs(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        assert "Recent Agent Runs" in card["body"][0]["text"]
        assert "1 total" in card["body"][1]["text"]

    async def test_caps_at_fifteen_with_more_footer(self, bot):
        ctx = make_ctx()
        runs = [
            {"id": f"run-{i}", "type": "resume", "status": "completed", "company": "C",
             "role": "R", "app_id": "app-001", "gdrive_folder_url": ""}
            for i in range(18)
        ]
        with patch("api_client.get_agent_runs", return_value=runs), \
             patch("api_client.get_applications", return_value=SAMPLE_APPS):
            await bot._cmd_runs(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        footer = card["body"][-1]
        assert footer["text"] == "…and 3 more."

    async def test_get_applications_error_renders_without_logo(self, bot):
        ctx = make_ctx()
        runs = [
            {"id": "run-1", "type": "resume", "status": "completed", "company": "Salesforce",
             "role": "SE", "app_id": "app-001", "gdrive_folder_url": ""},
        ]
        with patch("api_client.get_agent_runs", return_value=runs), \
             patch("api_client.get_applications", side_effect=Exception("down")):
            await bot._cmd_runs(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        row = card["body"][2]
        assert row["columns"][0]["items"][0]["type"] == "TextBlock"

    async def test_run_with_domain_includes_logo_column(self, bot):
        ctx = make_ctx()
        runs = [
            {"id": "run-1", "type": "resume", "status": "completed", "company": "Salesforce",
             "role": "SE", "app_id": "app-001", "gdrive_folder_url": ""},
        ]
        with patch("api_client.get_agent_runs", return_value=runs), \
             patch("api_client.get_applications", return_value=SAMPLE_APPS):
            await bot._cmd_runs(ctx, {"email": "a@b.com"})
        card = sent_cards(ctx)[0]
        row = card["body"][2]
        assert row["columns"][0]["items"][0]["type"] == "Image"


# ── _cmd_help ─────────────────────────────────────────────────────────────

class TestCmdHelp:
    async def test_sends_single_static_text_mentioning_key_commands(self, bot):
        ctx = make_ctx()
        await bot._cmd_help(ctx)
        texts = sent_texts(ctx)
        assert len(texts) == 1
        text = texts[0]
        for keyword in ("apply", "tracker", "cal today", "whoami"):
            assert keyword in text
