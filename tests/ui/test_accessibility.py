"""
Basic accessibility checks for key pages.
Tests that interactive elements have labels and forms are usable.
"""

import pytest
from playwright.sync_api import expect


class TestLoginAccessibility:
    def test_email_field_has_label(self, anon_page):
        anon_page.goto("/login.html")
        # Either an explicit label or a placeholder
        email = anon_page.locator("#email")
        label = anon_page.locator("label[for='email']")
        placeholder = email.get_attribute("placeholder")
        assert label.count() > 0 or (placeholder and len(placeholder) > 0)

    def test_password_field_has_label(self, anon_page):
        anon_page.goto("/login.html")
        password = anon_page.locator("#password")
        label = anon_page.locator("label[for='password']")
        placeholder = password.get_attribute("placeholder")
        assert label.count() > 0 or (placeholder and len(placeholder) > 0)

    def test_submit_button_has_text(self, anon_page):
        anon_page.goto("/login.html")
        btn = anon_page.locator("#submitBtn")
        text = btn.inner_text()
        assert len(text.strip()) > 0

    def test_page_has_title(self, anon_page):
        anon_page.goto("/login.html")
        title = anon_page.title()
        assert len(title) > 0


class TestAgentPageAccessibility:
    """#formCard starts collapsed — #formToggle must be clicked to reveal
    the job_posting/company/role fields inside #formBody."""

    def _expand_form(self, auth_page):
        auth_page.goto("/agents.html")
        auth_page.locator("#formToggle").click()
        auth_page.wait_for_timeout(300)

    def test_job_posting_field_has_label(self, auth_page):
        self._expand_form(auth_page)
        label = auth_page.locator("label[for='job_posting']")
        expect(label).to_be_visible()

    def test_company_field_has_label(self, auth_page):
        self._expand_form(auth_page)
        label = auth_page.locator("label[for='company']")
        expect(label).to_be_visible()

    def test_role_field_has_label(self, auth_page):
        self._expand_form(auth_page)
        label = auth_page.locator("label[for='role']")
        expect(label).to_be_visible()

    def test_all_buttons_have_accessible_text(self, auth_page):
        auth_page.goto("/agents.html")
        auth_page.wait_for_load_state("domcontentloaded")
        buttons = auth_page.locator("button:visible")
        for i in range(min(buttons.count(), 20)):
            btn = buttons.nth(i)
            text = btn.inner_text().strip()
            aria = btn.get_attribute("aria-label") or ""
            title = btn.get_attribute("title") or ""
            assert len(text) > 0 or len(aria) > 0 or len(title) > 0, \
                f"Button {i} has no accessible text"


class TestDarkMode:
    def test_dark_mode_toggle_on_login(self, anon_page):
        anon_page.goto("/login.html")
        toggle = anon_page.locator("#themeToggle")
        expect(toggle).to_be_visible()
        # Toggle should change the theme attribute
        before = anon_page.evaluate("document.documentElement.getAttribute('data-theme')")
        toggle.click()
        after = anon_page.evaluate("document.documentElement.getAttribute('data-theme')")
        assert before != after

    def test_dark_mode_persists_on_navigation(self, auth_page):
        auth_page.goto("/agents.html")
        # Set dark mode
        auth_page.evaluate("""
            document.documentElement.setAttribute('data-theme','dark');
            localStorage.setItem('theme','dark');
        """)
        # Navigate to another page
        auth_page.goto("/profile.html")
        auth_page.wait_for_load_state("domcontentloaded")
        theme = auth_page.evaluate("document.documentElement.getAttribute('data-theme')")
        assert theme == "dark"
