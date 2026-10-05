"""
Tests for Teams identity linking (_resolve_user, _cmd_confirm, _cmd_whoami,
_cmd_unlink, _offer_manual_link) and on_message_activity's command dispatch —
the auth subsystem Slack doesn't need (Slack identity comes from the
workspace token, not a per-message link lookup).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.teams.conftest import (
    make_ctx, make_entity_mention, make_file_attachment, sent_texts, sent_cards,
    SAMPLE_LINK_STATUS_LINKED, SAMPLE_LINK_STATUS_UNLINKED, SAMPLE_PROFILE,
)


def _member(email="a@b.com", upn=""):
    return MagicMock(email=email, user_principal_name=upn)


# ── _aad_object_id ────────────────────────────────────────────────────────

class TestAadObjectId:
    def test_returns_aad_object_id_when_present(self, bot):
        ctx = make_ctx(aad_object_id="aad-123")
        assert bot._aad_object_id(ctx) == "aad-123"

    def test_returns_none_when_absent(self, bot):
        ctx = make_ctx(aad_object_id=None)
        assert bot._aad_object_id(ctx) is None


# ── _resolve_user ─────────────────────────────────────────────────────────

class TestResolveUser:
    async def test_no_aad_object_id_sends_error(self, bot):
        ctx = make_ctx(aad_object_id=None)
        result = await bot._resolve_user(ctx)
        assert result is None
        assert "no Azure AD object id" in sent_texts(ctx)[0]

    async def test_link_status_error_sends_message(self, bot):
        ctx = make_ctx()
        with patch("api_client.teams_link_status", side_effect=Exception("boom")):
            result = await bot._resolve_user(ctx)
        assert result is None
        assert "Could not check your account link" in sent_texts(ctx)[0]

    async def test_linked_returns_email(self, bot):
        ctx = make_ctx()
        with patch("api_client.teams_link_status", return_value=SAMPLE_LINK_STATUS_LINKED):
            result = await bot._resolve_user(ctx)
        assert result == {"email": SAMPLE_LINK_STATUS_LINKED["email"]}

    async def test_unlinked_teams_email_lookup_error(self, bot):
        ctx = make_ctx()
        with patch("api_client.teams_link_status", return_value=SAMPLE_LINK_STATUS_UNLINKED), \
             patch.object(type(bot), "_teams_email", new=AsyncMock(side_effect=Exception("roster down"))):
            result = await bot._resolve_user(ctx)
        assert result is None
        assert "Could not look up your Teams profile" in sent_texts(ctx)[0]

    async def test_unlinked_no_email_found(self, bot):
        ctx = make_ctx()
        with patch("api_client.teams_link_status", return_value=SAMPLE_LINK_STATUS_UNLINKED), \
             patch.object(type(bot), "_teams_email", new=AsyncMock(return_value=None)):
            result = await bot._resolve_user(ctx)
        assert result is None
        assert "couldn't find an email address" in sent_texts(ctx)[0]

    async def test_unlinked_account_lookup_error(self, bot):
        ctx = make_ctx()
        with patch("api_client.teams_link_status", return_value=SAMPLE_LINK_STATUS_UNLINKED), \
             patch.object(type(bot), "_teams_email", new=AsyncMock(return_value="a@b.com")), \
             patch("api_client.teams_account_lookup", side_effect=Exception("down")):
            result = await bot._resolve_user(ctx)
        assert result is None
        assert "Error checking your account" in sent_texts(ctx)[0]

    async def test_unlinked_account_exists_prompts_confirm(self, bot):
        ctx = make_ctx()
        with patch("api_client.teams_link_status", return_value=SAMPLE_LINK_STATUS_UNLINKED), \
             patch.object(type(bot), "_teams_email", new=AsyncMock(return_value="a@b.com")), \
             patch("api_client.teams_account_lookup", return_value={"exists": True}):
            result = await bot._resolve_user(ctx)
        assert result is None
        assert "Reply **confirm**" in sent_texts(ctx)[0]

    async def test_unlinked_no_account_offers_manual_link(self, bot):
        ctx = make_ctx()
        with patch("api_client.teams_link_status", return_value=SAMPLE_LINK_STATUS_UNLINKED), \
             patch.object(type(bot), "_teams_email", new=AsyncMock(return_value="a@b.com")), \
             patch("api_client.teams_account_lookup", return_value={"exists": False}), \
             patch.object(type(bot), "_offer_manual_link", new=AsyncMock()) as mock_offer:
            result = await bot._resolve_user(ctx)
        assert result is None
        mock_offer.assert_awaited_once_with(ctx, ctx.activity.from_property.aad_object_id, "a@b.com")


# ── _offer_manual_link ────────────────────────────────────────────────────

class TestOfferManualLink:
    async def test_sends_signin_card_on_success(self, bot):
        ctx = make_ctx()
        with patch("api_client.teams_link_token", return_value="tok-abc"), \
             patch("api_client.Config") as mock_cfg:
            mock_cfg.API_BASE = "https://apply.cdlav.us"
            await bot._offer_manual_link(ctx, "aad-1", "a@b.com")
        ctx.send_activity.assert_awaited_once()
        activity = ctx.send_activity.call_args[0][0]
        assert activity.attachments[0].content_type.endswith("hero")

    async def test_token_failure_sends_error_text(self, bot):
        ctx = make_ctx()
        with patch("api_client.teams_link_token", side_effect=Exception("no token")):
            await bot._offer_manual_link(ctx, "aad-1", "a@b.com")
        text = sent_texts(ctx)[0]
        assert "couldn't generate a sign-in link" in text


# ── _cmd_confirm ──────────────────────────────────────────────────────────

class TestCmdConfirm:
    async def test_no_aad_object_id(self, bot):
        ctx = make_ctx(aad_object_id=None)
        await bot._cmd_confirm(ctx)
        assert "No Azure AD identity" in sent_texts(ctx)[0]

    async def test_teams_email_lookup_error(self, bot):
        ctx = make_ctx()
        with patch.object(type(bot), "_teams_email", new=AsyncMock(side_effect=Exception("boom"))):
            await bot._cmd_confirm(ctx)
        assert "Could not look up your Teams profile" in sent_texts(ctx)[0]

    async def test_no_email_found(self, bot):
        ctx = make_ctx()
        with patch.object(type(bot), "_teams_email", new=AsyncMock(return_value=None)):
            await bot._cmd_confirm(ctx)
        assert "No email address found" in sent_texts(ctx)[0]

    async def test_link_confirm_error(self, bot):
        ctx = make_ctx()
        with patch.object(type(bot), "_teams_email", new=AsyncMock(return_value="a@b.com")), \
             patch("api_client.teams_link_confirm", side_effect=Exception("down")):
            await bot._cmd_confirm(ctx)
        assert "Error linking your account" in sent_texts(ctx)[0]

    async def test_not_linked_offers_manual_link(self, bot):
        ctx = make_ctx()
        with patch.object(type(bot), "_teams_email", new=AsyncMock(return_value="a@b.com")), \
             patch("api_client.teams_link_confirm", return_value={"linked": False}), \
             patch.object(type(bot), "_offer_manual_link", new=AsyncMock()) as mock_offer:
            await bot._cmd_confirm(ctx)
        mock_offer.assert_awaited_once()

    async def test_success_sends_linked_message(self, bot):
        ctx = make_ctx()
        with patch.object(type(bot), "_teams_email", new=AsyncMock(return_value="a@b.com")), \
             patch("api_client.teams_link_confirm", return_value={"linked": True, "email": "a@b.com"}):
            await bot._cmd_confirm(ctx)
        assert "Linked as **a@b.com**" in sent_texts(ctx)[0]


# ── _cmd_whoami ───────────────────────────────────────────────────────────

class TestCmdWhoami:
    async def test_profile_fetch_error_falls_back_to_email(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_profile", side_effect=Exception("down")):
            await bot._cmd_whoami(ctx, {"email": "a@b.com"})
        assert "linked as **a@b.com**" in sent_texts(ctx)[0]

    async def test_success_renders_fact_card(self, bot):
        ctx = make_ctx()
        with patch("api_client.get_profile", return_value=SAMPLE_PROFILE), \
             patch("api_client.teams_link_status", return_value=SAMPLE_LINK_STATUS_LINKED):
            await bot._cmd_whoami(ctx, {"email": "a@b.com"})
        facts = {f["title"]: f["value"] for f in sent_cards(ctx)[0]["body"][2]["facts"]}
        assert facts["Email"] == "test@example.com"
        assert facts["Master Resume"] == "✅ master.docx"
        assert "Teams Link Expires" in facts

    async def test_no_profile_guide_shows_not_set(self, bot):
        ctx = make_ctx()
        profile = {**SAMPLE_PROFILE, "profile_text": ""}
        with patch("api_client.get_profile", return_value=profile), \
             patch("api_client.teams_link_status", side_effect=Exception("n/a")):
            await bot._cmd_whoami(ctx, {"email": "a@b.com"})
        facts = {f["title"]: f["value"] for f in sent_cards(ctx)[0]["body"][2]["facts"]}
        assert facts["Profile Guide"] == "❌ Not set"


# ── _cmd_unlink ───────────────────────────────────────────────────────────

class TestCmdUnlink:
    async def test_no_aad_object_id(self, bot):
        ctx = make_ctx(aad_object_id=None)
        await bot._cmd_unlink(ctx)
        assert "No Azure AD identity" in sent_texts(ctx)[0]

    async def test_unlink_error(self, bot):
        ctx = make_ctx()
        with patch("api_client.teams_unlink", side_effect=Exception("down")):
            await bot._cmd_unlink(ctx)
        assert "Error unlinking" in sent_texts(ctx)[0]

    async def test_success(self, bot):
        ctx = make_ctx()
        with patch("api_client.teams_unlink", return_value=None):
            await bot._cmd_unlink(ctx)
        assert "Unlinked" in sent_texts(ctx)[0]


# ── on_members_added_activity ────────────────────────────────────────────

class TestOnMembersAdded:
    async def test_sends_welcome_to_new_member(self, bot):
        ctx = make_ctx()
        from botbuilder.schema import ChannelAccount
        new_member = ChannelAccount(id="user-2")
        await bot.on_members_added_activity([new_member], ctx)
        assert "Welcome to Landed" in sent_texts(ctx)[0]

    async def test_does_not_welcome_the_bot_itself(self, bot):
        ctx = make_ctx()
        # ctx.activity.recipient.id defaults to "bot-1" (conftest.make_activity)
        bot_as_new_member = ctx.activity.recipient
        await bot.on_members_added_activity([bot_as_new_member], ctx)
        ctx.send_activity.assert_not_awaited()


# ── on_message_activity dispatch ─────────────────────────────────────────

class TestOnMessageActivityDispatch:
    async def test_help_bypasses_resolve_user(self, bot):
        ctx = make_ctx(text="help")
        with patch.object(type(bot), "_resolve_user", new=AsyncMock()) as mock_resolve, \
             patch.object(type(bot), "_cmd_help", new=AsyncMock()) as mock_help:
            await bot.on_message_activity(ctx)
        mock_help.assert_awaited_once()
        mock_resolve.assert_not_awaited()

    async def test_confirm_bypasses_resolve_user(self, bot):
        ctx = make_ctx(text="confirm")
        with patch.object(type(bot), "_resolve_user", new=AsyncMock()) as mock_resolve, \
             patch.object(type(bot), "_cmd_confirm", new=AsyncMock()) as mock_confirm:
            await bot.on_message_activity(ctx)
        mock_confirm.assert_awaited_once()
        mock_resolve.assert_not_awaited()

    async def test_unlink_bypasses_resolve_user(self, bot):
        ctx = make_ctx(text="unlink")
        with patch.object(type(bot), "_resolve_user", new=AsyncMock()) as mock_resolve, \
             patch.object(type(bot), "_cmd_unlink", new=AsyncMock()) as mock_unlink:
            await bot.on_message_activity(ctx)
        mock_unlink.assert_awaited_once()
        mock_resolve.assert_not_awaited()

    async def test_unresolved_user_stops_dispatch(self, bot):
        ctx = make_ctx(text="whoami")
        with patch.object(type(bot), "_resolve_user", new=AsyncMock(return_value=None)), \
             patch.object(type(bot), "_cmd_whoami", new=AsyncMock()) as mock_whoami:
            await bot.on_message_activity(ctx)
        mock_whoami.assert_not_awaited()

    async def test_whoami_dispatches_when_resolved(self, bot):
        ctx = make_ctx(text="whoami")
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_user", new=AsyncMock(return_value=user)), \
             patch.object(type(bot), "_cmd_whoami", new=AsyncMock()) as mock_whoami:
            await bot.on_message_activity(ctx)
        mock_whoami.assert_awaited_once_with(ctx, user)

    async def test_card_value_dispatches_to_handle_card_submit(self, bot):
        ctx = make_ctx(text="", value={"action": "apply_select_submit"})
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_user", new=AsyncMock(return_value=user)), \
             patch.object(type(bot), "_handle_card_submit", new=AsyncMock()) as mock_submit, \
             patch.object(type(bot), "_cmd_whoami", new=AsyncMock()) as mock_whoami:
            await bot.on_message_activity(ctx)
        mock_submit.assert_awaited_once_with(ctx, user)
        mock_whoami.assert_not_awaited()

    async def test_docx_attachment_short_circuits_command_dispatch(self, bot):
        ctx = make_ctx(text="whoami", attachments=[make_file_attachment()])
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_user", new=AsyncMock(return_value=user)), \
             patch.object(type(bot), "_handle_file_upload", new=AsyncMock(return_value=True)), \
             patch.object(type(bot), "_cmd_whoami", new=AsyncMock()) as mock_whoami:
            await bot.on_message_activity(ctx)
        mock_whoami.assert_not_awaited()

    async def test_non_file_attachment_falls_through_to_command(self, bot):
        """_handle_file_upload returning False (no matching .docx) must not
        swallow an ordinary text command riding along with unrelated
        attachment metadata (mentions, rich-text elements, etc.)."""
        ctx = make_ctx(text="whoami", attachments=[make_file_attachment()])
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_user", new=AsyncMock(return_value=user)), \
             patch.object(type(bot), "_handle_file_upload", new=AsyncMock(return_value=False)), \
             patch.object(type(bot), "_cmd_whoami", new=AsyncMock()) as mock_whoami:
            await bot.on_message_activity(ctx)
        mock_whoami.assert_awaited_once()

    async def test_mention_entity_stripped_before_matching(self, bot):
        ctx = make_ctx(text="<at>landed</at> whoami", entities=[make_entity_mention("<at>Landed</at>")])
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_user", new=AsyncMock(return_value=user)), \
             patch.object(type(bot), "_cmd_whoami", new=AsyncMock()) as mock_whoami:
            await bot.on_message_activity(ctx)
        mock_whoami.assert_awaited_once()

    async def test_unknown_command_sends_fallback(self, bot):
        ctx = make_ctx(text="do a barrel roll")
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_user", new=AsyncMock(return_value=user)):
            await bot.on_message_activity(ctx)
        assert "didn't recognise that command" in sent_texts(ctx)[0]

    async def test_track_list_extracts_status_filter(self, bot):
        ctx = make_ctx(text="track list interviewing")
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_user", new=AsyncMock(return_value=user)), \
             patch.object(type(bot), "_cmd_track_list", new=AsyncMock()) as mock_list:
            await bot.on_message_activity(ctx)
        mock_list.assert_awaited_once_with(ctx, "interviewing", user)

    async def test_bare_track_list_has_no_status_filter(self, bot):
        ctx = make_ctx(text="track list")
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_user", new=AsyncMock(return_value=user)), \
             patch.object(type(bot), "_cmd_track_list", new=AsyncMock()) as mock_list:
            await bot.on_message_activity(ctx)
        mock_list.assert_awaited_once_with(ctx, "", user)

    async def test_company_command_extracts_query(self, bot):
        # on_message_activity lowercases the whole text before dispatch.
        ctx = make_ctx(text="company Salesforce")
        user = {"email": "a@b.com"}
        with patch.object(type(bot), "_resolve_user", new=AsyncMock(return_value=user)), \
             patch.object(type(bot), "_cmd_company", new=AsyncMock()) as mock_company:
            await bot.on_message_activity(ctx)
        mock_company.assert_awaited_once_with(ctx, "salesforce")
