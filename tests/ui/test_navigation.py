"""UI tests for cross-page navigation and shared chrome."""

import pytest
from playwright.sync_api import expect


PAGES = [
    ("/agents.html",   "Agents - Landed"),
    ("/tracking.html", None),
    ("/calendar.html", None),
    ("/profile.html",  None),
]


class TestNavigation:
    @pytest.mark.parametrize("path,expected_title", PAGES)
    def test_page_loads_without_error(self, auth_page, path, expected_title):
        auth_page.goto(path)
        auth_page.wait_for_load_state("domcontentloaded")
        # No 404 / error page
        expect(auth_page.locator("body")).not_to_be_empty()
        if expected_title:
            expect(auth_page).to_have_title(expected_title)

    def test_agent_page_link_from_tracker(self, auth_page):
        auth_page.goto("/tracking.html")
        auth_page.wait_for_load_state("domcontentloaded")
        auth_page.locator("a[href='/agents.html']").first.click()
        auth_page.wait_for_url("**/agents.html", timeout=8_000)
        expect(auth_page).to_have_title("Agents - Landed")

    def test_tracker_link_from_agent_page(self, auth_page):
        auth_page.goto("/agents.html")
        auth_page.wait_for_load_state("domcontentloaded")
        auth_page.locator("a[href='/tracking.html']").first.click()
        auth_page.wait_for_url("**/tracking.html", timeout=8_000)

    def test_profile_link_from_agent_page(self, auth_page):
        auth_page.goto("/agents.html")
        auth_page.wait_for_load_state("domcontentloaded")
        auth_page.locator("a[href='/profile.html']").first.click()
        auth_page.wait_for_url("**/profile.html", timeout=8_000)

    def test_calendar_link_from_agent_page(self, auth_page):
        auth_page.goto("/agents.html")
        auth_page.wait_for_load_state("domcontentloaded")
        auth_page.locator("a[href='/calendar.html']").first.wait_for(state="visible", timeout=15_000)
        auth_page.locator("a[href='/calendar.html']").first.click()
        auth_page.wait_for_url("**/calendar.html", timeout=15_000)


class TestLogout:
    """Logout redirects to / (the public landing page), not /login.html —
    see frontend/agents.html's/profile.html's logoutBtn handlers."""

    def test_logout_from_agent_page(self, auth_page):
        auth_page.goto("/agents.html")
        auth_page.wait_for_selector("#logoutBtn", timeout=15_000)
        auth_page.click("#logoutBtn")
        auth_page.wait_for_url(lambda url: "agents.html" not in url, timeout=15_000)
        assert "login" not in auth_page.url
        expect(auth_page.locator(".header-actions a[href='/login.html']")).to_be_visible()

    def test_logout_from_profile_page(self, auth_page):
        auth_page.goto("/profile.html")
        auth_page.wait_for_selector("#logoutBtnHeader", timeout=8_000)
        auth_page.click("#logoutBtnHeader")
        auth_page.wait_for_url(lambda url: "profile.html" not in url, timeout=10_000)
        assert "login" not in auth_page.url
        expect(auth_page.locator(".header-actions a[href='/login.html']")).to_be_visible()
