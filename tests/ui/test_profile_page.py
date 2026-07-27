"""UI tests for /profile.html."""

import pytest
from playwright.sync_api import expect


class TestProfilePageStructure:
    def test_page_loads(self, auth_page):
        auth_page.goto("/profile.html")
        auth_page.wait_for_selector("#accountInfo", timeout=10_000)
        expect(auth_page.locator("#accountInfo")).not_to_be_empty()

    def test_shows_signed_in_email(self, auth_page):
        auth_page.goto("/profile.html")
        auth_page.wait_for_selector("#accountInfo", timeout=10_000)
        expect(auth_page.locator("#accountInfo")).to_contain_text("@")

    def test_account_card_present(self, auth_page):
        auth_page.goto("/profile.html")
        auth_page.wait_for_selector("#accountInfo", timeout=8_000)
        expect(auth_page.locator("#accountCard")).to_be_visible()

    def test_master_resume_card_present(self, auth_page):
        auth_page.goto("/profile.html")
        auth_page.wait_for_selector("#resumeStatus", timeout=8_000)
        expect(auth_page.locator("#resumeStatus")).to_be_visible()

    def test_profile_voice_guide_card_present(self, auth_page):
        auth_page.goto("/profile.html")
        auth_page.wait_for_selector("#profileCard", timeout=8_000)
        expect(auth_page.locator("#profileCard")).to_be_visible()

    def test_profile_text_renders_as_markdown(self, auth_page):
        """Profile text should render as formatted HTML, not raw markdown."""
        auth_page.goto("/profile.html")
        auth_page.wait_for_selector("#profilePreview", timeout=10_000)
        preview = auth_page.locator("#profilePreview")
        expect(preview).to_be_visible()
        # If profile has content, it should have HTML elements (not raw # symbols as text)
        content = preview.inner_html()
        if content and len(content) > 10:
            # Should NOT show raw markdown syntax as text
            assert "# Corey" not in preview.inner_text()

    def test_display_name_field_present(self, auth_page):
        auth_page.goto("/profile.html")
        auth_page.wait_for_selector("#display_name", timeout=8_000)
        expect(auth_page.locator("#display_name")).to_be_visible()

    def test_edit_account_button_present(self, auth_page):
        auth_page.goto("/profile.html")
        auth_page.wait_for_selector("#nameEditBtn", timeout=8_000)
        expect(auth_page.locator("#nameEditBtn")).to_be_visible()

    def test_edit_profile_button_present(self, auth_page):
        auth_page.goto("/profile.html")
        auth_page.wait_for_selector("#profileEditBtn", timeout=8_000)
        expect(auth_page.locator("#profileEditBtn")).to_be_visible()

    def test_logout_button_present(self, auth_page):
        auth_page.goto("/profile.html")
        auth_page.wait_for_selector("#logoutBtn", timeout=8_000)
        expect(auth_page.locator("#logoutBtn")).to_be_visible()
        expect(auth_page.locator("#logoutBtnHeader")).to_be_visible()


class TestProfileEditFlow:
    def test_edit_account_opens_name_field(self, auth_page):
        auth_page.goto("/profile.html")
        auth_page.wait_for_selector("#nameEditBtn", timeout=8_000)
        auth_page.click("#nameEditBtn")
        auth_page.wait_for_timeout(300)
        # Save and cancel buttons should appear
        expect(auth_page.locator("#nameBtn")).to_be_visible()
        expect(auth_page.locator("#nameCancelBtn")).to_be_visible()

    def test_cancel_account_edit_hides_save_button(self, auth_page):
        auth_page.goto("/profile.html")
        auth_page.wait_for_selector("#nameEditBtn", timeout=8_000)
        auth_page.click("#nameEditBtn")
        auth_page.wait_for_timeout(300)
        auth_page.click("#nameCancelBtn")
        auth_page.wait_for_timeout(300)
        expect(auth_page.locator("#nameBtn")).to_be_hidden()

    def test_edit_profile_shows_textarea(self, auth_page):
        auth_page.goto("/profile.html")
        auth_page.wait_for_load_state("networkidle", timeout=15_000)
        auth_page.click("#profileEditBtn")
        # Edit mode defaults to the guided wizard — opt into the raw
        # markdown editor to reach #profile_text / #mdToolbar.
        auth_page.click("#toRawModeLink")
        expect(auth_page.locator("#profile_text")).to_be_visible(timeout=8_000)
        expect(auth_page.locator("#mdToolbar")).to_be_visible()

    def test_edit_profile_shows_markdown_toolbar(self, auth_page):
        auth_page.goto("/profile.html")
        auth_page.wait_for_selector("#profileEditBtn", timeout=8_000)
        auth_page.click("#profileEditBtn")
        auth_page.click("#toRawModeLink")
        toolbar = auth_page.locator("#mdToolbar")
        expect(toolbar).to_be_visible(timeout=5_000)
        expect(toolbar.locator(".md-btn").first).to_be_visible(timeout=5_000)

    def test_cancel_profile_edit_hides_textarea(self, auth_page):
        auth_page.goto("/profile.html")
        auth_page.wait_for_selector("#profileEditBtn", timeout=8_000)
        auth_page.click("#profileEditBtn")
        auth_page.wait_for_timeout(300)
        auth_page.click("#profileCancelBtn")
        auth_page.wait_for_timeout(300)
        expect(auth_page.locator("#profile_text")).to_be_hidden()
        expect(auth_page.locator("#profilePreview")).to_be_visible()


class TestChangePassword:
    def test_change_password_form_present(self, auth_page):
        auth_page.goto("/profile.html")
        auth_page.wait_for_selector("#pwForm", timeout=8_000)
        expect(auth_page.locator("#current_password")).to_be_visible()
        expect(auth_page.locator("#new_password")).to_be_visible()
        expect(auth_page.locator("#pwBtn")).to_be_visible()

    def test_password_toast_element_exists(self, auth_page):
        auth_page.goto("/profile.html")
        auth_page.wait_for_selector("#pwForm", timeout=8_000)
        expect(auth_page.locator("#pwToast")).to_be_attached()
