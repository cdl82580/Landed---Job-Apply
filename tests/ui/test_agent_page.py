"""
UI tests for the agent page (agents.html).

Covers:
- Page structure and navigation
- Run form fields and validation
- Prep form fields, app picker, round type selector
- Past runs section
- Theme toggle
- Header elements
"""

from playwright.sync_api import expect

AGENT_PAGE = "/agents.html"


class TestAgentPageStructure:
    def test_page_title(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        expect(auth_page).to_have_title("Agents - Landed")

    def test_header_user_name_shown(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        auth_page.wait_for_selector("#headerUser", timeout=8_000)
        user_el = auth_page.locator("#headerUser")
        expect(user_el).not_to_be_empty()

    def test_nav_links_present(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        expect(auth_page.locator("a.header-icon-btn[href='/tracking.html']")).to_be_visible()
        expect(auth_page.locator("a.header-icon-btn[href='/calendar.html']")).to_be_visible()
        expect(auth_page.locator("a.header-icon-btn[href='/profile.html']")).to_be_visible()

    def test_theme_toggle_present(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        expect(auth_page.locator("#themeToggle")).to_be_visible()

    def test_theme_toggle_switches_theme(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        initial = auth_page.evaluate("document.documentElement.getAttribute('data-theme')")
        auth_page.click("#themeToggle")
        after = auth_page.evaluate("document.documentElement.getAttribute('data-theme')")
        assert initial != after

    def test_logout_button_present(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        expect(auth_page.locator("#logoutBtn")).to_be_visible()


class TestRunForm:
    """The "New Application" card (#formCard) starts collapsed — #formBody is
    hidden until #formToggle is clicked (or the page auto-expands it via a
    ?app_id=&autorun=1 redirect from the tracker)."""

    def _expand_form(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        auth_page.locator("#formToggle").click()
        auth_page.wait_for_timeout(300)

    def test_run_form_visible(self, auth_page):
        self._expand_form(auth_page)
        expect(auth_page.locator("#runForm")).to_be_visible()

    def test_job_posting_field_present(self, auth_page):
        self._expand_form(auth_page)
        expect(auth_page.locator("#job_posting")).to_be_visible()

    def test_company_and_role_fields_present(self, auth_page):
        self._expand_form(auth_page)
        expect(auth_page.locator("#company")).to_be_visible()
        expect(auth_page.locator("#role")).to_be_visible()

    def test_generate_button_present(self, auth_page):
        self._expand_form(auth_page)
        btn = auth_page.locator("#submitBtn").first
        expect(btn).to_be_visible()
        expect(btn).to_contain_text("Generate")

    def test_tracker_app_picker_present(self, auth_page):
        self._expand_form(auth_page)
        expect(auth_page.locator("#runAppSearch")).to_be_visible()

    def test_progress_card_hidden_by_default(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        expect(auth_page.locator("#progressCard")).to_be_hidden()

    def test_status_badge_exists(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        expect(auth_page.locator("#statusBadge")).to_be_attached()

    def test_new_run_button_exists(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        expect(auth_page.locator("#newRunBtn")).to_be_attached()


class TestPastRunsSection:
    def test_past_runs_card_present(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        expect(auth_page.locator("#pastRunsCard")).to_be_visible()

    def test_past_runs_toggle_works(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        toggle = auth_page.locator("#pastRunsToggle")
        expect(toggle).to_be_visible()
        body = auth_page.locator("#pastRunsBody")
        toggle.click()
        auth_page.wait_for_timeout(300)
        is_visible_after = body.is_visible()
        toggle.click()
        auth_page.wait_for_timeout(300)
        is_visible_after_2 = body.is_visible()
        assert is_visible_after != is_visible_after_2


class TestPrepForm:
    def test_prep_card_present(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        expect(auth_page.locator("#prepCard")).to_be_visible()

    def test_prep_toggle_expands_form(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        prep_toggle = auth_page.locator("#prepToggle")
        expect(prep_toggle).to_be_visible()
        prep_toggle.click()
        auth_page.wait_for_timeout(300)
        expect(auth_page.locator("#prepBody")).to_be_visible()

    def test_app_picker_visible_in_prep(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        auth_page.locator("#prepToggle").click()
        auth_page.wait_for_timeout(300)
        expect(auth_page.locator("#prepAppPickerWrap")).to_be_visible()

    def test_round_type_selector_has_options(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        auth_page.locator("#prepToggle").click()
        auth_page.wait_for_timeout(300)
        options = auth_page.locator("#prepRound option")
        expect(options).to_have_count(6)

    def test_prep_company_role_fields_present(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        auth_page.locator("#prepToggle").click()
        auth_page.wait_for_timeout(300)
        expect(auth_page.locator("#prepCompany")).to_be_visible()
        expect(auth_page.locator("#prepRole")).to_be_visible()

    def test_focus_slant_field_present(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        auth_page.locator("#prepToggle").click()
        auth_page.wait_for_timeout(300)
        expect(auth_page.locator("#prepFocus")).to_be_visible()

    def test_generate_prep_button_present(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        auth_page.locator("#prepToggle").click()
        auth_page.wait_for_timeout(300)
        expect(auth_page.locator("#prepSubmitBtn")).to_be_visible()
        expect(auth_page.locator("#prepSubmitBtn")).to_contain_text("Generate Prep Doc")

    def test_prep_empty_form_shows_error(self, auth_page):
        auth_page.goto(AGENT_PAGE)
        auth_page.locator("#prepToggle").click()
        auth_page.wait_for_timeout(300)
        auth_page.locator("#prepSubmitBtn").click()
        auth_page.wait_for_timeout(500)
        err = auth_page.locator("#prepFormErr")
        expect(err).to_be_visible()
        expect(err).not_to_be_empty()
