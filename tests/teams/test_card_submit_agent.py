"""
Tests for the agent-run card submission flow in teams_bot/bot.py:
_handle_card_submit dispatch, _resolve_app_and_jd / _jd_paste_card helpers,
the apply/prep/aq/thankyou select+final two-step handlers, and the
threaded long-running submits (_submit_apply, _submit_prep, _submit_aq,
_submit_thankyou, _submit_optimize, _submit_rescore).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.teams.conftest import make_ctx, sent_texts, sent_cards, SAMPLE_APP


NO_JD_APP = {
    "id": "app-002",
    "company": "Stripe",
    "role_title": "Staff Engineer",
    "domain": "stripe.com",
    "linked_runs": [],
}


def _card_all_text(card: dict) -> str:
    """Flatten every TextBlock's text anywhere in an Adaptive Card body,
    including nested Container/ColumnSet items, for substring assertions."""
    out = []

    def _walk(node):
        if isinstance(node, dict):
            if "text" in node:
                out.append(str(node["text"]))
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(card.get("body", []))
    return " ".join(out)


def _multi_run_app():
    return {
        "id": "app-009",
        "company": "Acme",
        "role_title": "Engineer",
        "domain": "acme.com",
        "linked_runs": [
            {"gdrive_folder_id": "folder-old", "linked_at": "2026-01-01T00:00:00Z", "type": "resume"},
            {"gdrive_folder_id": "folder-new", "linked_at": "2026-06-01T00:00:00Z", "type": "resume"},
        ],
    }


async def _fake_to_thread(func, *args, **kwargs):
    """Replacement for asyncio.to_thread that calls func inline instead of via
    a real ThreadPoolExecutor. _submit_optimize/_submit_rescore call
    asyncio.to_thread(api_client.get_application, ...) *and* later spawn a
    real threading.Thread for their background _run(); patching
    bot.threading.Thread with SyncThread replaces the global threading.Thread
    class for the whole `with` block, which breaks the real ThreadPoolExecutor
    asyncio.to_thread relies on internally (its worker threads are also
    threading.Thread instances). Patching bot.asyncio.to_thread with this
    avoids that collision."""
    return func(*args, **kwargs)


# ── _handle_card_submit dispatch ─────────────────────────────────────────

class TestHandleCardSubmitDispatch:
    async def test_apply_select_submit_routes_to_submit_apply_select(self, bot):
        ctx = make_ctx(value={"action": "apply_select_submit"})
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_submit_apply_select", new=AsyncMock()) as mock_handler:
            await bot._handle_card_submit(ctx, user)
        mock_handler.assert_awaited_once_with(ctx, ctx.activity.value, user)

    async def test_aq_final_submit_routes_to_submit_aq_final(self, bot):
        ctx = make_ctx(value={"action": "aq_final_submit"})
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_submit_aq_final", new=AsyncMock()) as mock_handler:
            await bot._handle_card_submit(ctx, user)
        mock_handler.assert_awaited_once_with(ctx, ctx.activity.value, user)

    async def test_track_delete_cancel_submit_sends_cancelled_text_directly(self, bot):
        ctx = make_ctx(value={"action": "track_delete_cancel_submit"})
        user = {"email": "a@b.com"}
        await bot._handle_card_submit(ctx, user)
        assert "Cancelled" in sent_texts(ctx)[0]

    async def test_unrecognized_action_sends_unknown_action(self, bot):
        ctx = make_ctx(value={"action": "totally_made_up_action"})
        user = {"email": "a@b.com"}
        await bot._handle_card_submit(ctx, user)
        assert "Unknown action: totally_made_up_action" in sent_texts(ctx)[0]


# ── _resolve_app_and_jd ───────────────────────────────────────────────────

class TestResolveAppAndJd:
    async def test_no_linked_runs_returns_none_jd(self, bot):
        user = {"email": "a@b.com"}
        with patch("api_client.get_application", return_value=NO_JD_APP):
            app, jd = await bot._resolve_app_and_jd("app-002", user)
        assert app == NO_JD_APP
        assert jd is None

    async def test_linked_run_with_jd_returns_text(self, bot):
        user = {"email": "a@b.com"}
        with patch("api_client.get_application", return_value=SAMPLE_APP), \
             patch("api_client.get_job_posting", return_value="Job description text") as mock_get_jd:
            app, jd = await bot._resolve_app_and_jd("app-001", user)
        assert app == SAMPLE_APP
        assert jd == "Job description text"
        mock_get_jd.assert_called_once_with("folder-1", user_email="a@b.com")

    async def test_picks_most_recently_linked_run(self, bot):
        user = {"email": "a@b.com"}
        multi = _multi_run_app()
        with patch("api_client.get_application", return_value=multi), \
             patch("api_client.get_job_posting", return_value="JD") as mock_get_jd:
            await bot._resolve_app_and_jd("app-009", user)
        mock_get_jd.assert_called_once_with("folder-new", user_email="a@b.com")

    async def test_get_job_posting_exception_is_swallowed(self, bot):
        user = {"email": "a@b.com"}
        with patch("api_client.get_application", return_value=SAMPLE_APP), \
             patch("api_client.get_job_posting", side_effect=Exception("drive down")):
            app, jd = await bot._resolve_app_and_jd("app-001", user)
        assert app == SAMPLE_APP
        assert jd is None


# ── _jd_paste_card ────────────────────────────────────────────────────────

class TestJdPasteCard:
    def test_action_submit_data_merges_action_and_extra_data(self, bot_module):
        card = bot_module.JobApplyBot._jd_paste_card("apply_final_submit", {"app_id": "app-1", "company": "Acme"})
        actions = [a for a in card["actions"] if a["type"] == "Action.Submit"]
        assert len(actions) == 1
        assert actions[0]["data"] == {"action": "apply_final_submit", "app_id": "app-1", "company": "Acme"}


# ── _submit_apply_select ──────────────────────────────────────────────────

class TestSubmitApplySelect:
    async def test_missing_app_id_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        await bot._submit_apply_select(ctx, {"app_id": ""}, user)
        assert "Please select an application" in sent_texts(ctx)[0]

    async def test_resolve_failure_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_app_and_jd", new=AsyncMock(side_effect=Exception("boom"))):
            await bot._submit_apply_select(ctx, {"app_id": "app-001"}, user)
        assert "Could not load application" in sent_texts(ctx)[0]

    async def test_found_jd_calls_submit_apply_with_merged_data(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_app_and_jd", new=AsyncMock(return_value=(SAMPLE_APP, "Saved JD text"))), \
             patch.object(type(bot), "_submit_apply", new=AsyncMock()) as mock_apply:
            await bot._submit_apply_select(ctx, {"app_id": "app-001", "contact": "Jane"}, user)
        mock_apply.assert_awaited_once_with(
            ctx,
            {"company": SAMPLE_APP["company"], "role": SAMPLE_APP["role_title"],
             "contact": "Jane", "job_posting": "Saved JD text", "domain": SAMPLE_APP["domain"]},
            user,
        )

    async def test_no_jd_sends_paste_card_with_carried_data(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_app_and_jd", new=AsyncMock(return_value=(SAMPLE_APP, None))):
            await bot._submit_apply_select(ctx, {"app_id": "app-001", "contact": "Jane"}, user)
        card = sent_cards(ctx)[0]
        action = next(a for a in card["actions"] if a["type"] == "Action.Submit")
        assert action["data"] == {
            "action": "apply_final_submit", "app_id": "app-001",
            "company": SAMPLE_APP["company"], "role": SAMPLE_APP["role_title"], "contact": "Jane",
            "domain": SAMPLE_APP["domain"],
        }


class TestSubmitApplyFinal:
    async def test_empty_job_posting_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        await bot._submit_apply_final(ctx, {"job_posting": "  "}, user)
        assert "Job posting is required" in sent_texts(ctx)[0]

    async def test_calls_submit_apply_with_merged_fields(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_submit_apply", new=AsyncMock()) as mock_apply:
            await bot._submit_apply_final(
                ctx, {"company": "Acme", "role": "Eng", "contact": "Jane", "job_posting": "JD text"}, user,
            )
        mock_apply.assert_awaited_once_with(
            ctx, {"company": "Acme", "role": "Eng", "contact": "Jane", "job_posting": "JD text",
                  "domain": ""}, user,
        )


# ── _submit_prep_select / _final ──────────────────────────────────────────

class TestSubmitPrepSelect:
    async def test_missing_app_id_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        await bot._submit_prep_select(ctx, {"app_id": "", "round_type": "technical"}, user)
        assert "Application and interview round are required" in sent_texts(ctx)[0]

    async def test_missing_round_type_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        await bot._submit_prep_select(ctx, {"app_id": "app-001", "round_type": ""}, user)
        assert "Application and interview round are required" in sent_texts(ctx)[0]

    async def test_resolve_failure_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_app_and_jd", new=AsyncMock(side_effect=Exception("boom"))):
            await bot._submit_prep_select(ctx, {"app_id": "app-001", "round_type": "technical"}, user)
        assert "Could not load application" in sent_texts(ctx)[0]

    async def test_found_jd_calls_submit_prep_with_merged_data(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_app_and_jd", new=AsyncMock(return_value=(SAMPLE_APP, "Saved JD"))), \
             patch.object(type(bot), "_submit_prep", new=AsyncMock()) as mock_prep:
            await bot._submit_prep_select(
                ctx, {"app_id": "app-001", "round_type": "technical", "interviewer": "Bob", "focus": "system design"}, user,
            )
        mock_prep.assert_awaited_once_with(
            ctx,
            {"company": SAMPLE_APP["company"], "role": SAMPLE_APP["role_title"], "round_type": "technical",
             "interviewer": "Bob", "focus": "system design", "job_posting": "Saved JD",
             "interview_date": "", "interview_time": "", "location": "", "domain": SAMPLE_APP["domain"]},
            user,
        )

    async def test_no_jd_sends_paste_card_with_carried_data(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_app_and_jd", new=AsyncMock(return_value=(SAMPLE_APP, None))):
            await bot._submit_prep_select(
                ctx, {"app_id": "app-001", "round_type": "technical", "interviewer": "Bob", "focus": "sd"}, user,
            )
        card = sent_cards(ctx)[0]
        action = next(a for a in card["actions"] if a["type"] == "Action.Submit")
        assert action["data"] == {
            "action": "prep_final_submit", "app_id": "app-001",
            "company": SAMPLE_APP["company"], "role": SAMPLE_APP["role_title"],
            "round_type": "technical", "interviewer": "Bob", "focus": "sd",
            "interview_date": "", "interview_time": "", "location": "", "domain": SAMPLE_APP["domain"],
        }


class TestSubmitPrepFinal:
    async def test_empty_job_posting_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        await bot._submit_prep_final(ctx, {"job_posting": ""}, user)
        assert "Job posting is required" in sent_texts(ctx)[0]

    async def test_calls_submit_prep_with_merged_fields(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_submit_prep", new=AsyncMock()) as mock_prep:
            await bot._submit_prep_final(
                ctx,
                {"company": "Acme", "role": "Eng", "round_type": "technical",
                 "interviewer": "Bob", "focus": "sd", "job_posting": "JD"},
                user,
            )
        mock_prep.assert_awaited_once_with(
            ctx,
            {"company": "Acme", "role": "Eng", "round_type": "technical",
             "interviewer": "Bob", "focus": "sd", "job_posting": "JD",
             "interview_date": "", "interview_time": "", "location": "", "domain": ""},
            user,
        )


# ── _submit_aq_select / _final ─────────────────────────────────────────────

class TestSubmitAqSelect:
    async def test_missing_app_id_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        await bot._submit_aq_select(ctx, {"app_id": "", "question": "Why us?"}, user)
        assert "Application and question are required" in sent_texts(ctx)[0]

    async def test_missing_question_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        await bot._submit_aq_select(ctx, {"app_id": "app-001", "question": ""}, user)
        assert "Application and question are required" in sent_texts(ctx)[0]

    async def test_resolve_failure_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_app_and_jd", new=AsyncMock(side_effect=Exception("boom"))):
            await bot._submit_aq_select(ctx, {"app_id": "app-001", "question": "Why us?"}, user)
        assert "Could not load application" in sent_texts(ctx)[0]

    async def test_found_jd_calls_submit_aq_with_merged_data(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_app_and_jd", new=AsyncMock(return_value=(SAMPLE_APP, "Saved JD"))), \
             patch.object(type(bot), "_submit_aq", new=AsyncMock()) as mock_aq:
            await bot._submit_aq_select(
                ctx, {"app_id": "app-001", "question": "Why us?", "tone": "casual", "char_limit": 500}, user,
            )
        mock_aq.assert_awaited_once_with(
            ctx,
            {"company": SAMPLE_APP["company"], "role": SAMPLE_APP["role_title"], "domain": SAMPLE_APP["domain"],
             "question": "Why us?", "tone": "casual", "char_limit": 500, "job_posting": "Saved JD"},
            user,
        )

    async def test_default_tone_is_professional(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_app_and_jd", new=AsyncMock(return_value=(SAMPLE_APP, "Saved JD"))), \
             patch.object(type(bot), "_submit_aq", new=AsyncMock()) as mock_aq:
            await bot._submit_aq_select(ctx, {"app_id": "app-001", "question": "Why us?"}, user)
        assert mock_aq.call_args[0][1]["tone"] == "professional"

    async def test_no_jd_sends_paste_card_with_carried_data(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_app_and_jd", new=AsyncMock(return_value=(SAMPLE_APP, None))):
            await bot._submit_aq_select(
                ctx, {"app_id": "app-001", "question": "Why us?", "tone": "casual", "char_limit": 500}, user,
            )
        card = sent_cards(ctx)[0]
        action = next(a for a in card["actions"] if a["type"] == "Action.Submit")
        assert action["data"] == {
            "action": "aq_final_submit", "app_id": "app-001",
            "company": SAMPLE_APP["company"], "role": SAMPLE_APP["role_title"], "domain": SAMPLE_APP["domain"],
            "question": "Why us?", "tone": "casual", "char_limit": 500,
        }


class TestSubmitAqFinal:
    async def test_empty_job_posting_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        await bot._submit_aq_final(ctx, {"job_posting": ""}, user)
        assert "Job posting is required" in sent_texts(ctx)[0]

    async def test_calls_submit_aq_with_merged_fields(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_submit_aq", new=AsyncMock()) as mock_aq:
            await bot._submit_aq_final(
                ctx,
                {"company": "Acme", "role": "Eng", "domain": "acme.com",
                 "question": "Why us?", "tone": "casual", "char_limit": 300, "job_posting": "JD"},
                user,
            )
        mock_aq.assert_awaited_once_with(
            ctx,
            {"company": "Acme", "role": "Eng", "domain": "acme.com",
             "question": "Why us?", "tone": "casual", "char_limit": 300, "job_posting": "JD"},
            user,
        )


# ── _submit_thankyou_select / _final ────────────────────────────────────────

class TestSubmitThankyouSelect:
    async def test_missing_app_id_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        await bot._submit_thankyou_select(ctx, {"app_id": "", "round_type": "onsite"}, user)
        assert "Application and interview round are required" in sent_texts(ctx)[0]

    async def test_missing_round_type_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        await bot._submit_thankyou_select(ctx, {"app_id": "app-001", "round_type": ""}, user)
        assert "Application and interview round are required" in sent_texts(ctx)[0]

    async def test_resolve_failure_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_app_and_jd", new=AsyncMock(side_effect=Exception("boom"))):
            await bot._submit_thankyou_select(ctx, {"app_id": "app-001", "round_type": "onsite"}, user)
        assert "Could not load application" in sent_texts(ctx)[0]

    async def test_found_jd_calls_submit_thankyou_with_app_id_carried(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_app_and_jd", new=AsyncMock(return_value=(SAMPLE_APP, "Saved JD"))), \
             patch.object(type(bot), "_submit_thankyou", new=AsyncMock()) as mock_ty:
            await bot._submit_thankyou_select(
                ctx,
                {"app_id": "app-001", "round_type": "onsite", "tone": "warm",
                 "interviewer": "Bob", "topics": "roadmap"},
                user,
            )
        mock_ty.assert_awaited_once_with(
            ctx,
            {"app_id": "app-001", "company": SAMPLE_APP["company"], "role": SAMPLE_APP["role_title"],
             "domain": SAMPLE_APP["domain"], "round_type": "onsite", "tone": "warm",
             "interviewer": "Bob", "topics": "roadmap", "job_posting": "Saved JD"},
            user,
        )

    async def test_no_jd_sends_paste_card_with_carried_data(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_app_and_jd", new=AsyncMock(return_value=(SAMPLE_APP, None))):
            await bot._submit_thankyou_select(
                ctx,
                {"app_id": "app-001", "round_type": "onsite", "tone": "warm",
                 "interviewer": "Bob", "topics": "roadmap"},
                user,
            )
        card = sent_cards(ctx)[0]
        action = next(a for a in card["actions"] if a["type"] == "Action.Submit")
        assert action["data"] == {
            "action": "thankyou_final_submit", "app_id": "app-001",
            "company": SAMPLE_APP["company"], "role": SAMPLE_APP["role_title"], "domain": SAMPLE_APP["domain"],
            "round_type": "onsite", "tone": "warm", "interviewer": "Bob", "topics": "roadmap",
        }


class TestSubmitThankyouFinal:
    async def test_empty_job_posting_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        await bot._submit_thankyou_final(ctx, {"job_posting": ""}, user)
        assert "Job posting is required" in sent_texts(ctx)[0]

    async def test_calls_submit_thankyou_with_app_id_carried_forward(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_submit_thankyou", new=AsyncMock()) as mock_ty:
            await bot._submit_thankyou_final(
                ctx,
                {"app_id": "app-001", "company": "Acme", "role": "Eng", "domain": "acme.com",
                 "round_type": "onsite", "tone": "warm", "interviewer": "Bob",
                 "topics": "roadmap", "job_posting": "JD"},
                user,
            )
        mock_ty.assert_awaited_once_with(
            ctx,
            {"app_id": "app-001", "company": "Acme", "role": "Eng", "domain": "acme.com",
             "round_type": "onsite", "tone": "warm", "interviewer": "Bob",
             "topics": "roadmap", "job_posting": "JD"},
            user,
        )


# ── _submit_apply (threaded) ──────────────────────────────────────────────

class TestSubmitApply:
    async def test_missing_required_fields_sends_error_no_thread(self, bot, bot_module):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread") as mock_thread:
            await bot._submit_apply(ctx, {"company": "", "role": "Eng", "job_posting": "JD"}, user)
        assert "Company, role, and job posting are required" in sent_texts(ctx)[0]
        mock_thread.assert_not_called()

    async def test_done_status_sends_success_proactive_message(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_run", return_value={"run_id": "run-1"}), \
             patch("api_client.poll_run", return_value={"status": "done"}), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_apply(ctx, {"company": "Acme", "role": "Eng", "contact": "", "job_posting": "JD"}, user)
        assert "Starting" in sent_texts(ctx)[0]
        assert "done" in mock_proactive.call_args[0][2]

    async def test_timeout_status_sends_timeout_proactive_message(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_run", return_value={"run_id": "run-1"}), \
             patch("api_client.poll_run", return_value={"status": "timeout"}), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_apply(ctx, {"company": "Acme", "role": "Eng", "job_posting": "JD"}, user)
        assert "longer than expected" in mock_proactive.call_args[0][2]

    async def test_error_status_sends_failure_proactive_message(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_run", return_value={"run_id": "run-1"}), \
             patch("api_client.poll_run", return_value={"status": "error", "error": "bad JD"}), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_apply(ctx, {"company": "Acme", "role": "Eng", "job_posting": "JD"}, user)
        assert "Run failed: bad JD" in mock_proactive.call_args[0][2]

    async def test_post_run_exception_sends_error_proactive_message(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_run", side_effect=Exception("network down")), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_apply(ctx, {"company": "Acme", "role": "Eng", "job_posting": "JD"}, user)
        msg = mock_proactive.call_args[0][2]
        assert "Error starting run" in msg
        assert "network down" not in msg  # raw exception text must not reach the chat


# ── _submit_prep (threaded) ────────────────────────────────────────────────

class TestSubmitPrep:
    async def test_missing_required_fields_sends_error_no_thread(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread") as mock_thread:
            await bot._submit_prep(ctx, {"company": "Acme", "role": "Eng", "round_type": "", "job_posting": "JD"}, user)
        assert "Company, role, round type, and job posting are required" in sent_texts(ctx)[0]
        mock_thread.assert_not_called()

    async def test_done_status_sends_success_proactive_message(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_prep", return_value={"prep_id": "prep-1"}), \
             patch("api_client.poll_prep", return_value={"status": "done"}), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_prep(
                ctx, {"company": "Acme", "role": "Eng", "round_type": "technical", "job_posting": "JD"}, user,
            )
        assert "Generating prep" in sent_texts(ctx)[0]
        assert "done" in mock_proactive.call_args[0][2]

    async def test_timeout_status_sends_timeout_proactive_message(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_prep", return_value={"prep_id": "prep-1"}), \
             patch("api_client.poll_prep", return_value={"status": "timeout"}), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_prep(
                ctx, {"company": "Acme", "role": "Eng", "round_type": "technical", "job_posting": "JD"}, user,
            )
        assert "longer than expected" in mock_proactive.call_args[0][2]

    async def test_error_status_sends_failure_proactive_message(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_prep", return_value={"prep_id": "prep-1"}), \
             patch("api_client.poll_prep", return_value={"status": "error", "error": "bad data"}), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_prep(
                ctx, {"company": "Acme", "role": "Eng", "round_type": "technical", "job_posting": "JD"}, user,
            )
        assert "Prep failed: bad data" in mock_proactive.call_args[0][2]

    async def test_post_prep_exception_sends_error_proactive_message(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_prep", side_effect=Exception("network down")), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_prep(
                ctx, {"company": "Acme", "role": "Eng", "round_type": "technical", "job_posting": "JD"}, user,
            )
        msg = mock_proactive.call_args[0][2]
        assert "Error" in msg
        assert "network down" not in msg  # raw exception text must not reach the chat


# ── _submit_aq (threaded) ──────────────────────────────────────────────────

class TestSubmitAq:
    async def test_missing_required_fields_sends_error_no_thread(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread") as mock_thread:
            await bot._submit_aq(ctx, {"company": "Acme", "role": "Eng", "question": "", "job_posting": "JD"}, user)
        assert "Company, role, question, and job posting are required" in sent_texts(ctx)[0]
        mock_thread.assert_not_called()

    async def test_done_status_sends_answer_card(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_aq", return_value={"aq_id": "aq-1"}), \
             patch("api_client.poll_aq", return_value={"status": "done", "answer": "My great answer"}), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_aq(
                ctx, {"company": "Acme", "role": "Eng", "domain": "acme.com", "question": "Why us?", "job_posting": "JD"}, user,
            )
        assert "Generating answer" in sent_texts(ctx)[0]
        card = mock_proactive.call_args.kwargs["card"]
        assert "My great answer" in _card_all_text(card)

    async def test_timeout_status_sends_timeout_text(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_aq", return_value={"aq_id": "aq-1"}), \
             patch("api_client.poll_aq", return_value={"status": "timeout"}), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_aq(
                ctx, {"company": "Acme", "role": "Eng", "question": "Why us?", "job_posting": "JD"}, user,
            )
        assert "longer than expected" in mock_proactive.call_args[0][2]

    async def test_error_status_sends_failure_text(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_aq", return_value={"aq_id": "aq-1"}), \
             patch("api_client.poll_aq", return_value={"status": "error", "error": "bad q"}), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_aq(
                ctx, {"company": "Acme", "role": "Eng", "question": "Why us?", "job_posting": "JD"}, user,
            )
        assert "Failed: bad q" in mock_proactive.call_args[0][2]

    async def test_post_aq_exception_sends_error_text(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_aq", side_effect=Exception("network down")), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_aq(
                ctx, {"company": "Acme", "role": "Eng", "question": "Why us?", "job_posting": "JD"}, user,
            )
        msg = mock_proactive.call_args[0][2]
        assert "Error" in msg
        assert "network down" not in msg  # raw exception text must not reach the chat


# ── _submit_thankyou (threaded) ────────────────────────────────────────────

class TestSubmitThankyou:
    async def test_missing_required_fields_sends_error_no_thread(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread") as mock_thread:
            await bot._submit_thankyou(
                ctx, {"company": "Acme", "role": "Eng", "round_type": "", "job_posting": "JD"}, user,
            )
        assert "Company, role, interview round, and job posting are required" in sent_texts(ctx)[0]
        mock_thread.assert_not_called()

    async def test_done_status_sends_success_proactive_message(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_thankyou", return_value={"ty_id": "ty-1"}), \
             patch("api_client.poll_thankyou", return_value={"status": "done"}), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_thankyou(
                ctx, {"company": "Acme", "role": "Eng", "round_type": "onsite", "job_posting": "JD"}, user,
            )
        assert "Generating thank-you email" in sent_texts(ctx)[0]
        assert "ready" in mock_proactive.call_args[0][2]

    async def test_timeout_status_sends_timeout_text(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_thankyou", return_value={"ty_id": "ty-1"}), \
             patch("api_client.poll_thankyou", return_value={"status": "timeout"}), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_thankyou(
                ctx, {"company": "Acme", "role": "Eng", "round_type": "onsite", "job_posting": "JD"}, user,
            )
        assert "longer than expected" in mock_proactive.call_args[0][2]

    async def test_error_status_sends_failure_text(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_thankyou", return_value={"ty_id": "ty-1"}), \
             patch("api_client.poll_thankyou", return_value={"status": "error", "error": "bad data"}), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_thankyou(
                ctx, {"company": "Acme", "role": "Eng", "round_type": "onsite", "job_posting": "JD"}, user,
            )
        assert "Failed: bad data" in mock_proactive.call_args[0][2]

    async def test_post_thankyou_exception_sends_error_text(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_thankyou", side_effect=Exception("network down")), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_thankyou(
                ctx, {"company": "Acme", "role": "Eng", "round_type": "onsite", "job_posting": "JD"}, user,
            )
        msg = mock_proactive.call_args[0][2]
        assert "Error" in msg
        assert "network down" not in msg  # raw exception text must not reach the chat


# ── _submit_optimize (threaded) ────────────────────────────────────────────

class TestSubmitOptimize:
    async def test_missing_app_id_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        await bot._submit_optimize(ctx, {"app_id": "", "instruction": "tighten it up"}, user)
        assert "Application and optimization prompt are required" in sent_texts(ctx)[0]

    async def test_missing_instruction_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        await bot._submit_optimize(ctx, {"app_id": "app-001", "instruction": ""}, user)
        assert "Application and optimization prompt are required" in sent_texts(ctx)[0]

    async def test_get_application_failure_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("api_client.get_application", side_effect=Exception("boom")):
            await bot._submit_optimize(ctx, {"app_id": "app-001", "instruction": "tighten it up"}, user)
        assert "Could not load application" in sent_texts(ctx)[0]

    async def test_no_linked_runs_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("api_client.get_application", return_value=NO_JD_APP):
            await bot._submit_optimize(ctx, {"app_id": "app-002", "instruction": "tighten it up"}, user)
        assert "has no linked Drive run folder" in sent_texts(ctx)[0]

    async def test_prefers_resume_type_folder_over_others(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        app = {
            "id": "app-010", "company": "Acme", "role_title": "Eng", "domain": "acme.com",
            "linked_runs": [
                {"gdrive_folder_id": "folder-newer-other", "linked_at": "2026-06-01T00:00:00Z", "type": "prep"},
                {"gdrive_folder_id": "folder-resume", "linked_at": "2026-01-01T00:00:00Z", "type": "resume"},
            ],
        }
        with patch("bot.asyncio.to_thread", _fake_to_thread), \
             patch("api_client.get_application", return_value=app), \
             patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_optimize", return_value={"optimize_id": "opt-1"}) as mock_post, \
             patch("api_client.poll_optimize", return_value={"status": "done"}), \
             patch.object(bot_module.JobApplyBot, "_proactive_message"):
            await bot._submit_optimize(ctx, {"app_id": "app-010", "instruction": "tighten it up"}, user)
        assert mock_post.call_args[0][1] == "folder-resume"

    async def test_falls_back_to_most_recent_when_no_preferred_type(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        app = {
            "id": "app-011", "company": "Acme", "role_title": "Eng", "domain": "acme.com",
            "linked_runs": [
                {"gdrive_folder_id": "folder-older", "linked_at": "2026-01-01T00:00:00Z", "type": "other"},
                {"gdrive_folder_id": "folder-newest", "linked_at": "2026-06-01T00:00:00Z", "type": "other"},
            ],
        }
        with patch("bot.asyncio.to_thread", _fake_to_thread), \
             patch("api_client.get_application", return_value=app), \
             patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_optimize", return_value={"optimize_id": "opt-1"}) as mock_post, \
             patch("api_client.poll_optimize", return_value={"status": "done"}), \
             patch.object(bot_module.JobApplyBot, "_proactive_message"):
            await bot._submit_optimize(ctx, {"app_id": "app-011", "instruction": "tighten it up"}, user)
        assert mock_post.call_args[0][1] == "folder-newest"

    async def test_done_status_sends_success_proactive_message(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.asyncio.to_thread", _fake_to_thread), \
             patch("api_client.get_application", return_value=SAMPLE_APP), \
             patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_optimize", return_value={"optimize_id": "opt-1"}), \
             patch("api_client.poll_optimize", return_value={"status": "done"}), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_optimize(ctx, {"app_id": "app-001", "instruction": "tighten it up"}, user)
        assert "Optimizing" in sent_texts(ctx)[0]
        assert "complete" in mock_proactive.call_args[0][2]

    async def test_timeout_status_sends_timeout_text(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.asyncio.to_thread", _fake_to_thread), \
             patch("api_client.get_application", return_value=SAMPLE_APP), \
             patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_optimize", return_value={"optimize_id": "opt-1"}), \
             patch("api_client.poll_optimize", return_value={"status": "timeout"}), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_optimize(ctx, {"app_id": "app-001", "instruction": "tighten it up"}, user)
        assert "longer than expected" in mock_proactive.call_args[0][2]

    async def test_error_status_sends_failure_text(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.asyncio.to_thread", _fake_to_thread), \
             patch("api_client.get_application", return_value=SAMPLE_APP), \
             patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_optimize", return_value={"optimize_id": "opt-1"}), \
             patch("api_client.poll_optimize", return_value={"status": "error", "error": "bad instruction"}), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_optimize(ctx, {"app_id": "app-001", "instruction": "tighten it up"}, user)
        assert "Optimization failed: bad instruction" in mock_proactive.call_args[0][2]

    async def test_post_optimize_exception_sends_error_text(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.asyncio.to_thread", _fake_to_thread), \
             patch("api_client.get_application", return_value=SAMPLE_APP), \
             patch("bot.threading.Thread", SyncThread), \
             patch("api_client.post_optimize", side_effect=Exception("network down")), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_optimize(ctx, {"app_id": "app-001", "instruction": "tighten it up"}, user)
        msg = mock_proactive.call_args[0][2]
        assert "Error" in msg
        assert "network down" not in msg  # raw exception text must not reach the chat


# ── _submit_rescore (threaded) ─────────────────────────────────────────────

class TestSubmitRescore:
    async def test_missing_app_id_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        await bot._submit_rescore(ctx, {"app_id": ""}, user)
        assert "Please select an application" in sent_texts(ctx)[0]

    async def test_get_application_failure_sends_error(self, bot):
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("api_client.get_application", side_effect=Exception("boom")):
            await bot._submit_rescore(ctx, {"app_id": "app-001"}, user)
        assert "Could not load application" in sent_texts(ctx)[0]

    async def test_success_sends_score_card(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.asyncio.to_thread", _fake_to_thread), \
             patch("api_client.get_application", return_value=SAMPLE_APP), \
             patch("bot.threading.Thread", SyncThread), \
             patch("api_client.score_application", return_value={
                 "score": 87, "category": "strong", "rationale": "Great alignment on platform work.",
             }), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_rescore(ctx, {"app_id": "app-001"}, user)
        assert "Scoring" in sent_texts(ctx)[0]
        card = mock_proactive.call_args.kwargs["card"]
        facts = {f["title"]: f["value"] for f in card["body"][2]["facts"]}
        assert facts["Score"] == "87/100"
        assert facts["Category"] == "Strong"
        assert "Great alignment on platform work." in _card_all_text(card)

    async def test_exception_with_response_json_detail_used(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}

        mock_response = MagicMock()
        mock_response.json.return_value = {"detail": "custom message"}
        exc = Exception("raw error")
        exc.response = mock_response

        with patch("bot.asyncio.to_thread", _fake_to_thread), \
             patch("api_client.get_application", return_value=SAMPLE_APP), \
             patch("bot.threading.Thread", SyncThread), \
             patch("api_client.score_application", side_effect=exc), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_rescore(ctx, {"app_id": "app-001"}, user)
        assert "Rescore failed: custom message" in mock_proactive.call_args[0][2]

    async def test_exception_without_response_falls_back_to_str(self, bot, bot_module):
        from tests.teams.conftest import SyncThread
        ctx = make_ctx()
        user = {"email": "a@b.com"}
        with patch("bot.asyncio.to_thread", _fake_to_thread), \
             patch("api_client.get_application", return_value=SAMPLE_APP), \
             patch("bot.threading.Thread", SyncThread), \
             patch("api_client.score_application", side_effect=Exception("plain failure")), \
             patch.object(bot_module.JobApplyBot, "_proactive_message") as mock_proactive:
            await bot._submit_rescore(ctx, {"app_id": "app-001"}, user)
        assert "Rescore failed: plain failure" in mock_proactive.call_args[0][2]
