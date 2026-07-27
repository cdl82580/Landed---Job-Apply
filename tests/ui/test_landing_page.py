"""UI tests for the public landing page at / — no auth required."""

from playwright.sync_api import expect


class TestLandingPageStructure:
    def test_page_loads_without_redirect(self, anon_page):
        """/ is intentionally public — an anonymous visitor should stay on it,
        not get bounced to /login.html."""
        anon_page.goto("/")
        assert "login" not in anon_page.url

    def test_title_present(self, anon_page):
        anon_page.goto("/")
        assert "Landed" in anon_page.title()

    def test_hero_heading_visible(self, anon_page):
        anon_page.goto("/")
        expect(anon_page.locator(".hero h1")).to_be_visible()

    def test_header_log_in_and_get_started_links(self, anon_page):
        anon_page.goto("/")
        expect(anon_page.locator(".header-actions a[href='/login.html']")).to_be_visible()
        expect(anon_page.locator(".header-actions a[href='/register.html']")).to_be_visible()

    def test_hero_cta_links(self, anon_page):
        anon_page.goto("/")
        expect(anon_page.locator(".hero .hero-actions a[href='/register.html']")).to_be_visible()
        expect(anon_page.locator(".hero .hero-actions a[href='/login.html']")).to_be_visible()

    def test_get_started_navigates_to_register(self, anon_page):
        anon_page.goto("/")
        anon_page.click(".hero .hero-actions a[href='/register.html']")
        anon_page.wait_for_url(lambda url: "register" in url, timeout=8_000)
