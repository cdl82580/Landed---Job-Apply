"""
Tests for the tracker/calendar/profile Adaptive Card submit handlers
(_submit_track_*, _submit_cal_*, _submit_profile_guide, _submit_notifications) —
the "step 2" handlers invoked after a user submits a card, as opposed to the
plain-text command handlers covered elsewhere.
"""

from unittest.mock import patch

import pytest

from tests.teams.conftest import make_ctx, sent_texts, sent_cards, SAMPLE_APP, SAMPLE_EVENT

USER = {"email": "a@b.com"}


# ── _submit_track_add ────────────────────────────────────────────────────

class TestSubmitTrackAdd:
    async def test_typeahead_pick_splits_company_and_domain(self, bot):
        ctx = make_ctx(value={"company": "Salesforce|||salesforce.com", "role": "Engineer"})
        with patch("api_client.create_application", return_value={}) as mock_create:
            await bot._submit_track_add(ctx, ctx.activity.value, USER)
        payload = mock_create.call_args.args[0]
        assert payload["company"] == "Salesforce"
        assert payload["domain"] == "salesforce.com"
        assert payload["role_title"] == "Engineer"

    async def test_freeform_company_has_no_domain(self, bot):
        ctx = make_ctx(value={"company": "Some Startup", "role": "Engineer"})
        with patch("api_client.create_application", return_value={}) as mock_create:
            await bot._submit_track_add(ctx, ctx.activity.value, USER)
        payload = mock_create.call_args.args[0]
        assert payload["company"] == "Some Startup"
        assert "domain" not in payload

    async def test_missing_company_sends_error_no_api_call(self, bot):
        ctx = make_ctx(value={"company": "", "role": "Engineer"})
        with patch("api_client.create_application") as mock_create:
            await bot._submit_track_add(ctx, ctx.activity.value, USER)
        mock_create.assert_not_called()
        assert "Company and role are required" in sent_texts(ctx)[0]

    async def test_missing_role_sends_error_no_api_call(self, bot):
        ctx = make_ctx(value={"company": "Salesforce", "role": ""})
        with patch("api_client.create_application") as mock_create:
            await bot._submit_track_add(ctx, ctx.activity.value, USER)
        mock_create.assert_not_called()
        assert "Company and role are required" in sent_texts(ctx)[0]

    async def test_optional_fields_only_included_when_non_empty(self, bot):
        ctx = make_ctx(value={
            "company": "Salesforce", "role": "Engineer",
            "url": "https://x.com", "location": "", "salary_range": "  ", "note": "hello",
        })
        with patch("api_client.create_application", return_value={}) as mock_create:
            await bot._submit_track_add(ctx, ctx.activity.value, USER)
        payload = mock_create.call_args.args[0]
        assert payload["url"] == "https://x.com"
        assert payload["note"] == "hello"
        assert "location" not in payload
        assert "salary_range" not in payload

    async def test_api_error_sends_error_message(self, bot):
        ctx = make_ctx(value={"company": "Salesforce", "role": "Engineer"})
        with patch("api_client.create_application", side_effect=Exception("boom")):
            await bot._submit_track_add(ctx, ctx.activity.value, USER)
        assert "Error" in sent_texts(ctx)[0]


# ── _submit_track_view ───────────────────────────────────────────────────

class TestSubmitTrackView:
    async def test_missing_app_id_sends_error(self, bot):
        ctx = make_ctx(value={"app_id": ""})
        with patch("api_client.get_application") as mock_get:
            await bot._submit_track_view(ctx, ctx.activity.value, USER)
        mock_get.assert_not_called()
        assert "No application selected" in sent_texts(ctx)[0]

    async def test_api_error_sends_error(self, bot):
        ctx = make_ctx(value={"app_id": "app-001"})
        with patch("api_client.get_application", side_effect=Exception("down")):
            await bot._submit_track_view(ctx, ctx.activity.value, USER)
        assert "Error" in sent_texts(ctx)[0]

    async def test_success_builds_factset_from_present_fields_only(self, bot):
        app = {
            **SAMPLE_APP, "location": "Remote", "salary_range": "$150k",
            "source": "", "recruiter_name": "Jane Smith",
        }
        ctx = make_ctx(value={"app_id": "app-001"})
        with patch("api_client.get_application", return_value=app):
            await bot._submit_track_view(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        factset = next(b for b in card["body"] if b["type"] == "FactSet")
        titles = {f["title"] for f in factset["facts"]}
        assert "Location" in titles
        assert "Salary" in titles
        assert "Applied" in titles
        assert "Recruiter" in titles
        assert "Source" not in titles

    async def test_header_columnset_has_company_role_status(self, bot):
        ctx = make_ctx(value={"app_id": "app-001"})
        with patch("api_client.get_application", return_value=SAMPLE_APP):
            await bot._submit_track_view(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        columnset = card["body"][0]
        assert columnset["type"] == "ColumnSet"
        texts = []
        for col in columnset["columns"]:
            for item in col.get("items", []):
                if item.get("type") == "TextBlock":
                    texts.append(item["text"])
        assert SAMPLE_APP["company"] in texts
        assert SAMPLE_APP["role_title"] in texts
        assert any(SAMPLE_APP["status"] in t for t in texts)

    async def test_notes_container_only_when_comments_present(self, bot):
        app_no_comments = {**SAMPLE_APP, "comments": []}
        ctx = make_ctx(value={"app_id": "app-001"})
        with patch("api_client.get_application", return_value=app_no_comments):
            await bot._submit_track_view(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        assert not any(
            b.get("type") == "Container" and any(
                i.get("text") == "Notes" for i in b.get("items", [])
            )
            for b in card["body"]
        )

    async def test_notes_container_present_and_capped_at_5(self, bot):
        comments = [{"text": f"note {i}"} for i in range(8)]
        app = {**SAMPLE_APP, "comments": comments}
        ctx = make_ctx(value={"app_id": "app-001"})
        with patch("api_client.get_application", return_value=app):
            await bot._submit_track_view(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        notes_container = next(
            b for b in card["body"]
            if b.get("type") == "Container" and any(i.get("text") == "Notes" for i in b.get("items", []))
        )
        note_items = [i for i in notes_container["items"] if i["text"] != "Notes"]
        assert len(note_items) == 5
        assert note_items[0]["text"] == "• note 3"
        assert note_items[-1]["text"] == "• note 7"

    async def test_open_job_posting_action_only_when_url_set(self, bot):
        app_with_url = {**SAMPLE_APP, "url": "https://salesforce.com/jobs/1", "linked_runs": []}
        ctx = make_ctx(value={"app_id": "app-001"})
        with patch("api_client.get_application", return_value=app_with_url):
            await bot._submit_track_view(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        titles = [a["title"] for a in card.get("actions", [])]
        assert "Open Job Posting" in titles

    async def test_no_url_no_open_job_posting_action(self, bot):
        app_no_url = {**SAMPLE_APP, "url": "", "linked_runs": []}
        ctx = make_ctx(value={"app_id": "app-001"})
        with patch("api_client.get_application", return_value=app_no_url):
            await bot._submit_track_view(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        titles = [a["title"] for a in card.get("actions", [])]
        assert "Open Job Posting" not in titles

    async def test_open_drive_folder_picks_most_recently_linked(self, bot):
        app = {
            **SAMPLE_APP,
            "url": "",
            "linked_runs": [
                {"folder_url": "https://drive.google.com/old", "linked_at": "2026-01-01T00:00:00Z"},
                {"folder_url": "https://drive.google.com/new", "linked_at": "2026-06-01T00:00:00Z"},
                {"folder_url": "", "linked_at": "2026-07-01T00:00:00Z"},
            ],
        }
        ctx = make_ctx(value={"app_id": "app-001"})
        with patch("api_client.get_application", return_value=app):
            await bot._submit_track_view(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        drive_action = next(a for a in card["actions"] if a["title"] == "Open Drive Folder")
        assert drive_action["url"] == "https://drive.google.com/new"

    async def test_no_linked_runs_with_folder_url_no_drive_action(self, bot):
        app = {**SAMPLE_APP, "url": "", "linked_runs": [{"gdrive_folder_id": "x"}]}
        ctx = make_ctx(value={"app_id": "app-001"})
        with patch("api_client.get_application", return_value=app):
            await bot._submit_track_view(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        titles = [a["title"] for a in card.get("actions", [])]
        assert "Open Drive Folder" not in titles


# ── _submit_track_update_select ──────────────────────────────────────────

class TestSubmitTrackUpdateSelect:
    async def test_missing_app_id_sends_error(self, bot):
        ctx = make_ctx(value={"app_id": ""})
        with patch("api_client.get_application") as mock_get:
            await bot._submit_track_update_select(ctx, ctx.activity.value, USER)
        mock_get.assert_not_called()
        assert "select an application" in sent_texts(ctx)[0]

    async def test_success_prefills_form_with_current_values(self, bot):
        ctx = make_ctx(value={"app_id": "app-001"})
        with patch("api_client.get_application", return_value=SAMPLE_APP):
            await bot._submit_track_update_select(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        status_input = next(b for b in card["body"] if b.get("id") == "status")
        assert status_input["value"] == SAMPLE_APP["status"]
        date_input = next(b for b in card["body"] if b.get("id") == "date_applied")
        assert date_input["value"] == SAMPLE_APP["date_applied"][:10]
        recruiter_input = next(b for b in card["body"] if b.get("id") == "recruiter_name")
        assert recruiter_input["value"] == SAMPLE_APP["recruiter_name"]

    async def test_no_date_applied_omits_date_value(self, bot):
        app = {**SAMPLE_APP, "date_applied": ""}
        ctx = make_ctx(value={"app_id": "app-001"})
        with patch("api_client.get_application", return_value=app):
            await bot._submit_track_update_select(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        date_input = next(b for b in card["body"] if b.get("id") == "date_applied")
        assert "value" not in date_input

    async def test_submit_action_targets_edit_submit_with_app_id(self, bot):
        ctx = make_ctx(value={"app_id": "app-001"})
        with patch("api_client.get_application", return_value=SAMPLE_APP):
            await bot._submit_track_update_select(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        submit_action = card["actions"][0]
        assert submit_action["data"] == {"action": "track_update_edit_submit", "app_id": "app-001"}


# ── _submit_track_update_edit ─────────────────────────────────────────────

class TestSubmitTrackUpdateEdit:
    async def test_missing_app_id_sends_error(self, bot):
        ctx = make_ctx(value={"app_id": ""})
        with patch("api_client.update_application") as mock_update:
            await bot._submit_track_update_edit(ctx, ctx.activity.value, USER)
        mock_update.assert_not_called()
        assert "Missing application reference" in sent_texts(ctx)[0]

    async def test_blank_optional_fields_become_none_and_are_stripped(self, bot):
        ctx = make_ctx(value={
            "app_id": "app-001", "status": "Applied",
            "job_source": "", "location": "  ", "salary_range": "", "url": "",
            "recruiter_name": "", "recruiter_email": "",
        })
        with patch("api_client.update_application", return_value=SAMPLE_APP) as mock_update:
            await bot._submit_track_update_edit(ctx, ctx.activity.value, USER)
        updates = mock_update.call_args.args[1]
        assert "job_source" not in updates
        assert "location" not in updates
        assert "salary_range" not in updates
        assert "url" not in updates
        assert "recruiter_name" not in updates
        assert "recruiter_email" not in updates
        assert updates["status"] == "Applied"

    async def test_empty_tags_included_to_clear(self, bot):
        ctx = make_ctx(value={"app_id": "app-001", "status": "Applied"})
        with patch("api_client.update_application", return_value=SAMPLE_APP) as mock_update:
            await bot._submit_track_update_edit(ctx, ctx.activity.value, USER)
        updates = mock_update.call_args.args[1]
        assert updates["tags"] == []

    async def test_tags_parsed_from_comma_list(self, bot):
        ctx = make_ctx(value={"app_id": "app-001", "status": "Applied", "tags": "DUA Reported, Referral ,"})
        with patch("api_client.update_application", return_value=SAMPLE_APP) as mock_update:
            await bot._submit_track_update_edit(ctx, ctx.activity.value, USER)
        updates = mock_update.call_args.args[1]
        assert updates["tags"] == ["DUA Reported", "Referral"]

    async def test_date_applied_gets_time_suffix(self, bot):
        ctx = make_ctx(value={"app_id": "app-001", "status": "Applied", "date_applied": "2026-07-01"})
        with patch("api_client.update_application", return_value=SAMPLE_APP) as mock_update:
            await bot._submit_track_update_edit(ctx, ctx.activity.value, USER)
        updates = mock_update.call_args.args[1]
        assert updates["date_applied"] == "2026-07-01T00:00:00Z"

    async def test_blank_date_applied_not_included(self, bot):
        ctx = make_ctx(value={"app_id": "app-001", "status": "Applied", "date_applied": ""})
        with patch("api_client.update_application", return_value=SAMPLE_APP) as mock_update:
            await bot._submit_track_update_edit(ctx, ctx.activity.value, USER)
        updates = mock_update.call_args.args[1]
        assert "date_applied" not in updates

    async def test_non_empty_note_triggers_add_comment(self, bot):
        ctx = make_ctx(value={"app_id": "app-001", "status": "Applied", "note": "Called recruiter"})
        with patch("api_client.update_application", return_value=SAMPLE_APP), \
             patch("api_client.add_comment") as mock_comment:
            await bot._submit_track_update_edit(ctx, ctx.activity.value, USER)
        mock_comment.assert_called_once_with("app-001", "Called recruiter", user_email="a@b.com")

    async def test_empty_note_does_not_call_add_comment(self, bot):
        ctx = make_ctx(value={"app_id": "app-001", "status": "Applied", "note": ""})
        with patch("api_client.update_application", return_value=SAMPLE_APP), \
             patch("api_client.add_comment") as mock_comment:
            await bot._submit_track_update_edit(ctx, ctx.activity.value, USER)
        mock_comment.assert_not_called()

    async def test_update_error_sends_failed_message(self, bot):
        ctx = make_ctx(value={"app_id": "app-001", "status": "Applied"})
        with patch("api_client.update_application", side_effect=Exception("boom")):
            await bot._submit_track_update_edit(ctx, ctx.activity.value, USER)
        assert "Failed to update" in sent_texts(ctx)[0]


# ── _submit_track_note ────────────────────────────────────────────────────

class TestSubmitTrackNote:
    async def test_missing_app_id_sends_error_no_api_calls(self, bot):
        ctx = make_ctx(value={"app_id": "", "note": "hi"})
        with patch("api_client.get_application") as mock_get, \
             patch("api_client.add_comment") as mock_comment:
            await bot._submit_track_note(ctx, ctx.activity.value, USER)
        mock_get.assert_not_called()
        mock_comment.assert_not_called()
        assert "Application and note are required" in sent_texts(ctx)[0]

    async def test_missing_note_sends_error_no_api_calls(self, bot):
        ctx = make_ctx(value={"app_id": "app-001", "note": ""})
        with patch("api_client.get_application") as mock_get, \
             patch("api_client.add_comment") as mock_comment:
            await bot._submit_track_note(ctx, ctx.activity.value, USER)
        mock_get.assert_not_called()
        mock_comment.assert_not_called()
        assert "Application and note are required" in sent_texts(ctx)[0]

    async def test_success_calls_get_and_add_comment_confirms_with_note_text(self, bot):
        ctx = make_ctx(value={"app_id": "app-001", "note": "Great chat with recruiter"})
        with patch("api_client.get_application", return_value=SAMPLE_APP) as mock_get, \
             patch("api_client.add_comment", return_value={}) as mock_comment:
            await bot._submit_track_note(ctx, ctx.activity.value, USER)
        mock_get.assert_called_once()
        mock_comment.assert_called_once_with("app-001", "Great chat with recruiter", user_email="a@b.com")
        text = sent_texts(ctx)[0]
        assert "Great chat with recruiter" in text

    async def test_get_application_error_sends_failed_message(self, bot):
        ctx = make_ctx(value={"app_id": "app-001", "note": "hi"})
        with patch("api_client.get_application", side_effect=Exception("boom")), \
             patch("api_client.add_comment") as mock_comment:
            await bot._submit_track_note(ctx, ctx.activity.value, USER)
        assert "Failed to add note" in sent_texts(ctx)[0]

    async def test_add_comment_error_sends_failed_message(self, bot):
        ctx = make_ctx(value={"app_id": "app-001", "note": "hi"})
        with patch("api_client.get_application", return_value=SAMPLE_APP), \
             patch("api_client.add_comment", side_effect=Exception("boom")):
            await bot._submit_track_note(ctx, ctx.activity.value, USER)
        assert "Failed to add note" in sent_texts(ctx)[0]


# ── _submit_track_delete_select ──────────────────────────────────────────

class TestSubmitTrackDeleteSelect:
    async def test_missing_app_id_sends_error(self, bot):
        ctx = make_ctx(value={"app_id": ""})
        with patch("api_client.get_application") as mock_get:
            await bot._submit_track_delete_select(ctx, ctx.activity.value, USER)
        mock_get.assert_not_called()
        assert "select an application" in sent_texts(ctx)[0]

    async def test_success_sends_confirmation_card_with_actions(self, bot):
        ctx = make_ctx(value={"app_id": "app-001"})
        with patch("api_client.get_application", return_value=SAMPLE_APP):
            await bot._submit_track_delete_select(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        delete_action = next(a for a in card["actions"] if a["title"] == "Delete")
        cancel_action = next(a for a in card["actions"] if a["title"] == "Cancel")
        assert delete_action["style"] == "destructive"
        assert delete_action["data"]["action"] == "track_delete_confirm_submit"
        assert cancel_action["data"]["action"] == "track_delete_cancel_submit"


# ── _submit_track_delete_confirm ─────────────────────────────────────────

class TestSubmitTrackDeleteConfirm:
    async def test_missing_app_id_sends_error(self, bot):
        ctx = make_ctx(value={"app_id": ""})
        with patch("api_client.get_application") as mock_get, \
             patch("api_client.delete_application") as mock_delete:
            await bot._submit_track_delete_confirm(ctx, ctx.activity.value, USER)
        mock_get.assert_not_called()
        mock_delete.assert_not_called()
        assert "Missing application reference" in sent_texts(ctx)[0]

    async def test_success_calls_get_then_delete_sends_confirmation(self, bot):
        ctx = make_ctx(value={"app_id": "app-001"})
        with patch("api_client.get_application", return_value=SAMPLE_APP) as mock_get, \
             patch("api_client.delete_application", return_value=None) as mock_delete:
            await bot._submit_track_delete_confirm(ctx, ctx.activity.value, USER)
        mock_get.assert_called_once()
        mock_delete.assert_called_once()
        assert "Deleted" in sent_texts(ctx)[0]

    async def test_error_sends_failed_message(self, bot):
        ctx = make_ctx(value={"app_id": "app-001"})
        with patch("api_client.get_application", side_effect=Exception("boom")):
            await bot._submit_track_delete_confirm(ctx, ctx.activity.value, USER)
        assert "Failed to delete" in sent_texts(ctx)[0]


# ── _submit_cal_add ───────────────────────────────────────────────────────

class TestSubmitCalAdd:
    async def test_missing_title_sends_error(self, bot):
        ctx = make_ctx(value={"title": "", "event_date": "2026-07-10"})
        with patch("api_client.create_calendar_event") as mock_create:
            await bot._submit_cal_add(ctx, ctx.activity.value, USER)
        mock_create.assert_not_called()
        assert "Title and date are required" in sent_texts(ctx)[0]

    async def test_missing_date_sends_error(self, bot):
        ctx = make_ctx(value={"title": "Interview", "event_date": ""})
        with patch("api_client.create_calendar_event") as mock_create:
            await bot._submit_cal_add(ctx, ctx.activity.value, USER)
        mock_create.assert_not_called()
        assert "Title and date are required" in sent_texts(ctx)[0]

    async def test_malformed_time_sends_invalid_time_error(self, bot):
        ctx = make_ctx(value={"title": "Interview", "event_date": "2026-07-10", "event_time": "not-a-time"})
        with patch("api_client.create_calendar_event") as mock_create:
            await bot._submit_cal_add(ctx, ctx.activity.value, USER)
        mock_create.assert_not_called()
        assert "Invalid time format" in sent_texts(ctx)[0]

    async def test_duration_clamped_above_max(self, bot):
        ctx = make_ctx(value={"title": "Interview", "event_date": "2026-07-10", "duration": "9999"})
        with patch("api_client.create_calendar_event", return_value=SAMPLE_EVENT) as mock_create:
            await bot._submit_cal_add(ctx, ctx.activity.value, USER)
        payload = mock_create.call_args.args[0]
        assert payload["duration_minutes"] == 1440

    async def test_duration_clamped_below_min(self, bot):
        ctx = make_ctx(value={"title": "Interview", "event_date": "2026-07-10", "duration": "-50"})
        with patch("api_client.create_calendar_event", return_value=SAMPLE_EVENT) as mock_create:
            await bot._submit_cal_add(ctx, ctx.activity.value, USER)
        payload = mock_create.call_args.args[0]
        assert payload["duration_minutes"] == 0

    async def test_missing_duration_defaults_to_60(self, bot):
        ctx = make_ctx(value={"title": "Interview", "event_date": "2026-07-10"})
        with patch("api_client.create_calendar_event", return_value=SAMPLE_EVENT) as mock_create:
            await bot._submit_cal_add(ctx, ctx.activity.value, USER)
        payload = mock_create.call_args.args[0]
        assert payload["duration_minutes"] == 60

    async def test_non_numeric_duration_defaults_to_60(self, bot):
        ctx = make_ctx(value={"title": "Interview", "event_date": "2026-07-10", "duration": "abc"})
        with patch("api_client.create_calendar_event", return_value=SAMPLE_EVENT) as mock_create:
            await bot._submit_cal_add(ctx, ctx.activity.value, USER)
        payload = mock_create.call_args.args[0]
        assert payload["duration_minutes"] == 60

    async def test_reminder_added_only_when_offset_and_email_reminder_true(self, bot):
        ctx = make_ctx(value={
            "title": "Interview", "event_date": "2026-07-10",
            "reminder_offset": "1440", "reminder_email": "true",
        })
        with patch("api_client.create_calendar_event", return_value=SAMPLE_EVENT) as mock_create:
            await bot._submit_cal_add(ctx, ctx.activity.value, USER)
        payload = mock_create.call_args.args[0]
        assert payload["reminders"] == [{"offset_minutes": 1440, "channels": ["email"]}]

    async def test_reminder_omitted_when_email_reminder_false(self, bot):
        ctx = make_ctx(value={
            "title": "Interview", "event_date": "2026-07-10",
            "reminder_offset": "1440", "reminder_email": "false",
        })
        with patch("api_client.create_calendar_event", return_value=SAMPLE_EVENT) as mock_create:
            await bot._submit_cal_add(ctx, ctx.activity.value, USER)
        payload = mock_create.call_args.args[0]
        assert payload["reminders"] == []

    async def test_reminder_omitted_when_offset_missing(self, bot):
        ctx = make_ctx(value={
            "title": "Interview", "event_date": "2026-07-10",
            "reminder_email": "true",
        })
        with patch("api_client.create_calendar_event", return_value=SAMPLE_EVENT) as mock_create:
            await bot._submit_cal_add(ctx, ctx.activity.value, USER)
        payload = mock_create.call_args.args[0]
        assert payload["reminders"] == []

    async def test_unknown_event_type_falls_back_to_custom(self, bot):
        ctx = make_ctx(value={"title": "Interview", "event_date": "2026-07-10", "event_type": "bogus"})
        with patch("api_client.create_calendar_event", return_value=SAMPLE_EVENT) as mock_create:
            await bot._submit_cal_add(ctx, ctx.activity.value, USER)
        payload = mock_create.call_args.args[0]
        assert payload["event_type"] == "custom"

    async def test_known_event_type_preserved(self, bot):
        ctx = make_ctx(value={"title": "Interview", "event_date": "2026-07-10", "event_type": "interview"})
        with patch("api_client.create_calendar_event", return_value=SAMPLE_EVENT) as mock_create:
            await bot._submit_cal_add(ctx, ctx.activity.value, USER)
        payload = mock_create.call_args.args[0]
        assert payload["event_type"] == "interview"

    async def test_api_error_sends_failed_to_create_message(self, bot):
        ctx = make_ctx(value={"title": "Interview", "event_date": "2026-07-10"})
        with patch("api_client.create_calendar_event", side_effect=Exception("boom")):
            await bot._submit_cal_add(ctx, ctx.activity.value, USER)
        assert "Failed to create event" in sent_texts(ctx)[0]


# ── _submit_cal_view ──────────────────────────────────────────────────────

class TestSubmitCalView:
    async def test_missing_event_id_sends_error(self, bot):
        ctx = make_ctx(value={"event_id": ""})
        with patch("api_client.get_calendar_event") as mock_get:
            await bot._submit_cal_view(ctx, ctx.activity.value, USER)
        mock_get.assert_not_called()
        assert "No event selected" in sent_texts(ctx)[0]

    async def test_api_error_sends_could_not_load(self, bot):
        ctx = make_ctx(value={"event_id": "event-1"})
        with patch("api_client.get_calendar_event", side_effect=Exception("boom")):
            await bot._submit_cal_view(ctx, ctx.activity.value, USER)
        assert "Could not load event" in sent_texts(ctx)[0]

    async def test_success_includes_duration_fact_when_truthy(self, bot):
        ctx = make_ctx(value={"event_id": "event-1"})
        with patch("api_client.get_calendar_event", return_value=SAMPLE_EVENT):
            await bot._submit_cal_view(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        factset = next(b for b in card["body"] if b["type"] == "FactSet")
        titles = {f["title"] for f in factset["facts"]}
        assert "Duration" in titles

    async def test_no_duration_omits_duration_fact(self, bot):
        event = {**SAMPLE_EVENT, "duration_minutes": 0}
        ctx = make_ctx(value={"event_id": "event-1"})
        with patch("api_client.get_calendar_event", return_value=event):
            await bot._submit_cal_view(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        factset = next(b for b in card["body"] if b["type"] == "FactSet")
        titles = {f["title"] for f in factset["facts"]}
        assert "Duration" not in titles

    async def test_reminder_fact_formatted_in_minutes(self, bot):
        event = {**SAMPLE_EVENT, "reminders": [{"offset_minutes": 30, "channels": ["email"]}]}
        ctx = make_ctx(value={"event_id": "event-1"})
        with patch("api_client.get_calendar_event", return_value=event):
            await bot._submit_cal_view(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        factset = next(b for b in card["body"] if b["type"] == "FactSet")
        reminder_fact = next(f for f in factset["facts"] if "Reminder" in f["title"])
        assert "30m" in reminder_fact["value"]

    async def test_reminder_fact_formatted_in_hours(self, bot):
        event = {**SAMPLE_EVENT, "reminders": [{"offset_minutes": 120, "channels": ["email"]}]}
        ctx = make_ctx(value={"event_id": "event-1"})
        with patch("api_client.get_calendar_event", return_value=event):
            await bot._submit_cal_view(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        factset = next(b for b in card["body"] if b["type"] == "FactSet")
        reminder_fact = next(f for f in factset["facts"] if "Reminder" in f["title"])
        assert "2h" in reminder_fact["value"]

    async def test_reminder_fact_formatted_in_days(self, bot):
        event = {**SAMPLE_EVENT, "reminders": [{"offset_minutes": 1440, "channels": ["email"]}]}
        ctx = make_ctx(value={"event_id": "event-1"})
        with patch("api_client.get_calendar_event", return_value=event):
            await bot._submit_cal_view(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        factset = next(b for b in card["body"] if b["type"] == "FactSet")
        reminder_fact = next(f for f in factset["facts"] if "Reminder" in f["title"])
        assert "1d" in reminder_fact["value"]

    async def test_notes_container_present_when_notes_set(self, bot):
        ctx = make_ctx(value={"event_id": "event-1"})
        with patch("api_client.get_calendar_event", return_value=SAMPLE_EVENT):
            await bot._submit_cal_view(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        assert any(
            b.get("type") == "Container" and any(i.get("text") == "Notes" for i in b.get("items", []))
            for b in card["body"]
        )

    async def test_notes_container_absent_when_no_notes(self, bot):
        event = {**SAMPLE_EVENT, "notes": ""}
        ctx = make_ctx(value={"event_id": "event-1"})
        with patch("api_client.get_calendar_event", return_value=event):
            await bot._submit_cal_view(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        assert not any(
            b.get("type") == "Container" and any(i.get("text") == "Notes" for i in b.get("items", []))
            for b in card["body"]
        )


# ── _submit_cal_delete_select / _submit_cal_delete_confirm ──────────────

class TestSubmitCalDeleteSelect:
    async def test_missing_event_id_sends_error(self, bot):
        ctx = make_ctx(value={"event_id": ""})
        with patch("api_client.get_calendar_event") as mock_get:
            await bot._submit_cal_delete_select(ctx, ctx.activity.value, USER)
        mock_get.assert_not_called()
        assert "select an event" in sent_texts(ctx)[0]

    async def test_success_sends_confirmation_card_with_actions(self, bot):
        ctx = make_ctx(value={"event_id": "event-1"})
        with patch("api_client.get_calendar_event", return_value=SAMPLE_EVENT):
            await bot._submit_cal_delete_select(ctx, ctx.activity.value, USER)
        card = sent_cards(ctx)[0]
        delete_action = next(a for a in card["actions"] if a["title"] == "Delete")
        cancel_action = next(a for a in card["actions"] if a["title"] == "Cancel")
        assert delete_action["style"] == "destructive"
        assert delete_action["data"]["action"] == "cal_delete_confirm_submit"
        assert cancel_action["data"]["action"] == "cal_delete_cancel_submit"


class TestSubmitCalDeleteConfirm:
    async def test_missing_event_id_sends_error(self, bot):
        ctx = make_ctx(value={"event_id": ""})
        with patch("api_client.delete_calendar_event") as mock_delete:
            await bot._submit_cal_delete_confirm(ctx, ctx.activity.value, USER)
        mock_delete.assert_not_called()
        assert "Missing event reference" in sent_texts(ctx)[0]

    async def test_success_deletes_and_confirms(self, bot):
        ctx = make_ctx(value={"event_id": "event-1"})
        with patch("api_client.delete_calendar_event", return_value=None) as mock_delete:
            await bot._submit_cal_delete_confirm(ctx, ctx.activity.value, USER)
        mock_delete.assert_called_once()
        assert "deleted" in sent_texts(ctx)[0].lower()

    async def test_error_sends_failed_to_delete(self, bot):
        ctx = make_ctx(value={"event_id": "event-1"})
        with patch("api_client.delete_calendar_event", side_effect=Exception("boom")):
            await bot._submit_cal_delete_confirm(ctx, ctx.activity.value, USER)
        assert "Failed to delete" in sent_texts(ctx)[0]


# ── _submit_profile_guide ────────────────────────────────────────────────

class TestSubmitProfileGuide:
    async def test_success_calls_update_profile_and_confirms(self, bot):
        ctx = make_ctx(value={"guide": "Be direct, no fluff."})
        with patch("api_client.update_profile", return_value={}) as mock_update:
            await bot._submit_profile_guide(ctx, ctx.activity.value, USER)
        mock_update.assert_called_once_with(
            {"profile_text": "Be direct, no fluff."}, user_email="a@b.com"
        )
        assert "saved" in sent_texts(ctx)[0].lower()

    async def test_missing_guide_defaults_to_empty_string(self, bot):
        ctx = make_ctx(value={})
        with patch("api_client.update_profile", return_value={}) as mock_update:
            await bot._submit_profile_guide(ctx, ctx.activity.value, USER)
        mock_update.assert_called_once_with({"profile_text": ""}, user_email="a@b.com")

    async def test_error_sends_failure_message(self, bot):
        ctx = make_ctx(value={"guide": "text"})
        with patch("api_client.update_profile", side_effect=Exception("boom")):
            await bot._submit_profile_guide(ctx, ctx.activity.value, USER)
        assert "Failed to save guide" in sent_texts(ctx)[0]


# ── _submit_notifications ────────────────────────────────────────────────

class TestSubmitNotifications:
    async def test_comma_joined_string_prefs(self, bot):
        ctx = make_ctx(value={"prefs": "daily_digest,status_changed"})
        with patch("api_client.update_profile", return_value={}) as mock_update:
            await bot._submit_notifications(ctx, ctx.activity.value, USER)
        prefs = mock_update.call_args.args[0]["notification_prefs"]
        assert prefs["daily_digest"] is True
        assert prefs["status_changed"] is True
        assert prefs["weekly_digest"] is False
        assert prefs["researching_nudge"] is False

    async def test_list_shape_prefs_same_result_as_string(self, bot):
        ctx = make_ctx(value={"prefs": ["daily_digest", "status_changed"]})
        with patch("api_client.update_profile", return_value={}) as mock_update:
            await bot._submit_notifications(ctx, ctx.activity.value, USER)
        prefs = mock_update.call_args.args[0]["notification_prefs"]
        assert prefs["daily_digest"] is True
        assert prefs["status_changed"] is True
        assert prefs["weekly_digest"] is False

    async def test_tuple_shape_prefs_accepted(self, bot):
        ctx = make_ctx(value={"prefs": ("daily_digest",)})
        with patch("api_client.update_profile", return_value={}) as mock_update:
            await bot._submit_notifications(ctx, ctx.activity.value, USER)
        prefs = mock_update.call_args.args[0]["notification_prefs"]
        assert prefs["daily_digest"] is True

    async def test_confirmation_lists_on_and_off_labels_trimmed(self, bot):
        ctx = make_ctx(value={"prefs": "daily_digest"})
        with patch("api_client.update_profile", return_value={}):
            await bot._submit_notifications(ctx, ctx.activity.value, USER)
        text = sent_texts(ctx)[0]
        assert "On:" in text
        assert "Daily digest" in text
        assert "Off:" in text
        assert "Weekly digest" in text
        assert "—" not in text.split("On:")[1].split("Off:")[0]

    async def test_error_sends_failed_message_no_confirmation(self, bot):
        ctx = make_ctx(value={"prefs": "daily_digest"})
        with patch("api_client.update_profile", side_effect=Exception("boom")):
            await bot._submit_notifications(ctx, ctx.activity.value, USER)
        texts = sent_texts(ctx)
        assert len(texts) == 1
        assert "Failed to save preferences" in texts[0]
