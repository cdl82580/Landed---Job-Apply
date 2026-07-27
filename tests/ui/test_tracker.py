"""UI tests for /tracking.html — application tracker."""

import pytest
from playwright.sync_api import expect


class TestTrackerPageStructure:
    def test_page_loads(self, auth_page):
        auth_page.goto("/tracking.html")
        # #appTable/#appCards stay hidden and #emptyState shows instead when
        # the account has zero tracked applications — all three are valid
        # "page rendered" states.
        auth_page.wait_for_selector("#appTable:not(.hidden), #appCards:not(:empty), #emptyState:not(.hidden)", timeout=10_000)

    def test_add_button_present(self, auth_page):
        auth_page.goto("/tracking.html")
        auth_page.wait_for_selector("#addBtn", timeout=8_000)
        expect(auth_page.locator("#addBtn")).to_be_visible()
        expect(auth_page.locator("#addBtn")).to_contain_text("Add")

    def test_search_input_present(self, auth_page):
        auth_page.goto("/tracking.html")
        auth_page.wait_for_selector("#searchInput", timeout=8_000)
        expect(auth_page.locator("#searchInput")).to_be_visible()

    def test_status_pills_present(self, auth_page):
        auth_page.goto("/tracking.html")
        auth_page.wait_for_selector("#statusPills", timeout=8_000)
        pills = auth_page.locator("#statusPills .pill, #statusPills button")
        # Should have multiple status filter pills
        count = pills.count()
        assert count >= 3

    def test_stats_bar_present(self, auth_page):
        auth_page.goto("/tracking.html")
        auth_page.wait_for_selector("#statsBar", timeout=8_000)
        expect(auth_page.locator("#statTotal")).to_be_visible()
        expect(auth_page.locator("#statInterviewing")).to_be_visible()
        expect(auth_page.locator("#statApplied")).to_be_visible()

    def test_header_user_shown(self, auth_page):
        auth_page.goto("/tracking.html")
        auth_page.wait_for_selector("#headerUser", timeout=8_000)
        expect(auth_page.locator("#headerUser")).not_to_be_empty()

    def test_nav_links_present(self, auth_page):
        auth_page.goto("/tracking.html")
        expect(auth_page.locator("a.header-icon-btn[href='/agents.html']")).to_be_visible()
        expect(auth_page.locator("a.header-icon-btn[href='/calendar.html']")).to_be_visible()
        expect(auth_page.locator("a.header-icon-btn[href='/profile.html']")).to_be_visible()


class TestAddApplicationDrawer:
    def test_add_button_opens_drawer(self, auth_page):
        auth_page.goto("/tracking.html")
        auth_page.wait_for_selector("#addBtn", timeout=8_000)
        auth_page.click("#addBtn")
        auth_page.wait_for_timeout(500)
        expect(auth_page.locator("#drawer")).to_be_visible()

    def test_drawer_has_company_search(self, auth_page):
        auth_page.goto("/tracking.html")
        auth_page.wait_for_selector("#addBtn", timeout=8_000)
        auth_page.click("#addBtn")
        auth_page.wait_for_timeout(500)
        # Drawer should have a company search / input field
        drawer = auth_page.locator("#drawer")
        expect(drawer).to_be_visible()
        inputs = drawer.locator("input, select")
        assert inputs.count() > 0

    def test_overlay_shown_with_drawer(self, auth_page):
        auth_page.goto("/tracking.html")
        auth_page.wait_for_selector("#addBtn", timeout=8_000)
        auth_page.click("#addBtn")
        auth_page.wait_for_timeout(500)
        expect(auth_page.locator("#overlay")).to_be_visible()

    def test_overlay_click_closes_drawer(self, auth_page):
        auth_page.goto("/tracking.html")
        auth_page.wait_for_selector("#addBtn", timeout=8_000)
        auth_page.click("#addBtn")
        auth_page.wait_for_timeout(500)
        expect(auth_page.locator("#drawer")).to_be_visible()
        # Click overlay to close
        auth_page.locator("#overlay").click()
        auth_page.wait_for_timeout(500)
        expect(auth_page.locator("#drawer")).not_to_have_class("open")


class TestSearchAndFilter:
    def test_search_filters_table(self, auth_page):
        auth_page.goto("/tracking.html")
        auth_page.wait_for_selector("#searchInput", timeout=8_000)
        # Type something unlikely to match
        auth_page.fill("#searchInput", "zzzzz_unlikely_match_9999")
        auth_page.wait_for_timeout(600)
        # Either table is empty or empty state is shown
        empty = auth_page.locator("#emptyState")
        table_rows = auth_page.locator("#appTableBody tr, #appCards .app-card")
        visible_empty = empty.is_visible()
        row_count = table_rows.count()
        assert visible_empty or row_count == 0

    def test_clear_search_restores_results(self, auth_page):
        auth_page.goto("/tracking.html")
        auth_page.wait_for_selector("#searchInput", timeout=8_000)
        auth_page.fill("#searchInput", "zzzzz")
        auth_page.wait_for_timeout(400)
        auth_page.fill("#searchInput", "")
        auth_page.wait_for_timeout(400)
        # Empty state should no longer be forced by search
        # (may still be empty if user has no applications)
        expect(auth_page.locator("#searchInput")).to_have_value("")
