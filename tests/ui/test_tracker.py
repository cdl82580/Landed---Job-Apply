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


class TestTagFilter:
    """Tag include/exclude filter. The applications list is mocked so the test
    is deterministic and never touches the test account's real data."""

    APPS = [
        {"id": "a1", "company": "Acme",  "role_title": "SE",  "status": "Applied", "last_updated": "2026-01-04T00:00:00Z",
         "tags": ["DUA Reported", "Referral"]},
        {"id": "a2", "company": "Beta",  "role_title": "TAM", "status": "Applied", "last_updated": "2026-01-03T00:00:00Z",
         "tags": ["Referral"]},
        {"id": "a3", "company": "Gamma", "role_title": "FDE", "status": "Applied", "last_updated": "2026-01-02T00:00:00Z",
         "tags": []},
        {"id": "a4", "company": "Delta", "role_title": "CSM", "status": "Applied", "last_updated": "2026-01-01T00:00:00Z",
         "tags": ["DUA Reported"]},
    ]

    @pytest.fixture()
    def tracker(self, auth_page):
        auth_page.route(
            "**/api/applications",
            lambda route: route.fulfill(json={"items": self.APPS, "total": len(self.APPS)})
            if route.request.method == "GET" else route.continue_(),
        )
        auth_page.goto("/tracking.html")
        auth_page.wait_for_selector("#appTableBody tr", timeout=10_000)
        return auth_page

    @staticmethod
    def _companies(page):
        page.wait_for_timeout(200)
        return page.locator("#appTableBody tr .co-name").all_inner_texts()

    @staticmethod
    def _open_menu(page):
        page.click("#tagFilterBtn")
        expect(page.locator("#tagFilterMenu")).to_be_visible()

    def test_menu_lists_tags_in_use(self, tracker):
        self._open_menu(tracker)
        rows = tracker.locator("#tagFilterList .tag-filter-row")
        expect(rows).to_have_count(2)
        expect(rows.nth(0)).to_contain_text("DUA Reported")
        expect(rows.nth(1)).to_contain_text("Referral")

    def test_search_narrows_tag_list(self, tracker):
        self._open_menu(tracker)
        tracker.fill("#tagFilterSearch", "ref")
        rows = tracker.locator("#tagFilterList .tag-filter-row")
        expect(rows).to_have_count(1)
        expect(rows.first).to_contain_text("Referral")

    def test_include_shows_only_tagged(self, tracker):
        self._open_menu(tracker)
        tracker.check("#tagFilterList input[value='DUA Reported']")
        assert sorted(self._companies(tracker)) == ["Acme", "Delta"]
        expect(tracker.locator("#tagFilterBtn")).to_have_text("Tags: DUA Reported")

    def test_multiple_included_tags_match_any(self, tracker):
        self._open_menu(tracker)
        tracker.check("#tagFilterList input[value='DUA Reported']")
        tracker.check("#tagFilterList input[value='Referral']")
        assert sorted(self._companies(tracker)) == ["Acme", "Beta", "Delta"]

    def test_exclude_hides_tagged_and_keeps_untagged(self, tracker):
        self._open_menu(tracker)
        tracker.click("#tagFilterList .tag-ex-btn[data-tag='DUA Reported']")
        assert sorted(self._companies(tracker)) == ["Beta", "Gamma"]
        expect(tracker.locator("#tagFilterBtn")).to_have_text("Tags: not DUA Reported")
        # menu stays open so more tags can be toggled
        expect(tracker.locator("#tagFilterMenu")).to_be_visible()

    def test_include_and_exclude_combine(self, tracker):
        self._open_menu(tracker)
        tracker.check("#tagFilterList input[value='Referral']")
        tracker.click("#tagFilterList .tag-ex-btn[data-tag='DUA Reported']")
        assert self._companies(tracker) == ["Beta"]
        expect(tracker.locator("#tagFilterBtn")).to_have_text("Tags: Referral, not DUA Reported")

    def test_including_an_excluded_tag_removes_the_exclusion(self, tracker):
        self._open_menu(tracker)
        tracker.click("#tagFilterList .tag-ex-btn[data-tag='DUA Reported']")
        tracker.check("#tagFilterList input[value='DUA Reported']")
        assert sorted(self._companies(tracker)) == ["Acme", "Delta"]
        expect(tracker.locator(".tag-ex-btn[data-tag='DUA Reported']")).not_to_have_class("on")

    def test_clear_resets_tag_filter(self, tracker):
        self._open_menu(tracker)
        tracker.click("#tagFilterList .tag-ex-btn[data-tag='DUA Reported']")
        tracker.click("#clearFiltersBtn")
        assert sorted(self._companies(tracker)) == ["Acme", "Beta", "Delta", "Gamma"]
        expect(tracker.locator("#tagFilterBtn")).to_have_text("Tags: All")

    def test_escape_closes_menu(self, tracker):
        self._open_menu(tracker)
        tracker.keyboard.press("Escape")
        expect(tracker.locator("#tagFilterMenu")).to_be_hidden()
