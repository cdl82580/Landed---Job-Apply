"""Regression tests: HTML-escaping of user/application-supplied text in
notification emails.

Every email built from display_name/company/role/status/location/etc. used
to interpolate that text straight into the HTML body. Since company/role can
come from a scraped job posting rather than something the user typed
themselves, and since a compromised or careless third-party job listing is
exactly the kind of input a security review should assume is hostile, these
tests plant a `"><script>alert(1)</script>` style payload in each field and
assert the raw payload never reaches the HTML the email actually sends —
only its escaped form should appear.
"""

import api
from scripts import notif_dispatch

PAYLOAD = '"><script>alert(1)</script>'
ESCAPED = "&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"


class TestEscHtmlHelper:
    def test_escapes_all_html_special_chars(self):
        assert api._esc_html(PAYLOAD) == ESCAPED
        assert notif_dispatch._esc_html(PAYLOAD) == ESCAPED

    def test_handles_none_and_empty(self):
        assert api._esc_html(None) == ""
        assert api._esc_html("") == ""

    def test_handles_non_string_input(self):
        # ms["score"] etc. may be a number — must stringify before escaping.
        assert api._esc_html(42) == "42"


class TestApiEmailHelpers:
    """api.py's HTML-building helpers, called directly with hostile input."""

    def test_digest_status_pill_escapes_status(self):
        html_out = api._digest_status_pill(PAYLOAD)
        assert PAYLOAD not in html_out
        assert ESCAPED in html_out

    def test_digest_company_cell_escapes_company_and_logo_url(self):
        html_out = api._digest_company_cell({"company": PAYLOAD, "company_logo_url": PAYLOAD})
        assert PAYLOAD not in html_out
        assert ESCAPED in html_out

    def test_app_logo_html_escapes_url(self):
        html_out = api._app_logo_html({"company_logo_url": PAYLOAD})
        assert PAYLOAD not in html_out
        assert ESCAPED in html_out

    def test_company_heading_html_escapes_title(self):
        html_out = api._company_heading_html({}, f"Did you apply to {PAYLOAD}?")
        assert PAYLOAD not in html_out
        assert ESCAPED in html_out


class TestNotifDispatchEmailHelpers:
    """scripts/notif_dispatch.py's HTML-building helpers — a second,
    independent implementation of the same emails, so it needs its own
    coverage rather than assuming api.py's fix carries over."""

    def test_status_pill_escapes_status(self):
        html_out = notif_dispatch._status_pill(PAYLOAD)
        assert PAYLOAD not in html_out
        assert ESCAPED in html_out

    def test_company_logo_html_escapes_url(self):
        html_out = notif_dispatch._company_logo_html({"company_logo_url": PAYLOAD})
        assert PAYLOAD not in html_out
        assert ESCAPED in html_out


class TestVerificationEmailEscaping:
    def test_display_name_escaped_in_html_not_in_text(self, monkeypatch):
        captured = {}

        def fake_send_email(to, subject, body, html=None):
            captured["text"] = body
            captured["html"] = html
            return True

        monkeypatch.setattr(api, "_send_email", fake_send_email)
        api._send_verification_email("user@example.com", PAYLOAD, "tok123")

        assert PAYLOAD not in captured["html"]
        assert ESCAPED in captured["html"]
        # The plain-text part is never rendered as HTML, so it must stay
        # un-escaped (escaping it would show literal "&quot;" to the reader).
        assert PAYLOAD in captured["text"]


class TestReminderEmailEscaping:
    def test_event_title_escaped_in_html(self, monkeypatch):
        captured = {}

        def fake_send_email(to, subject, body, html=None):
            captured["html"] = html
            return True

        monkeypatch.setattr(api, "_send_email", fake_send_email)
        monkeypatch.setattr(api, "_NOTIFY_EMAIL", "user@example.com")

        event = {"title": PAYLOAD, "datetime": "2026-01-01T12:00:00", "timezone": "UTC"}
        reminder = {"channels": ["email"], "offset_minutes": 30}
        api._fire_reminder("user-1", reminder, event)

        assert PAYLOAD not in captured["html"]
        assert ESCAPED in captured["html"]


class TestApplicationNudgeEmailsEscaping:
    """_researching_nudge_email / _follow_up_reminder_email / _gone_silent_email
    all build a body_html around company/role — check each independently
    since they're separate functions, not a shared code path."""

    def _capture(self, monkeypatch):
        captured = {}

        def fake_send_email(to, subject, body, html=None):
            captured["html"] = html
            return True

        monkeypatch.setattr(api, "_send_email", fake_send_email)
        return captured

    def test_researching_nudge_escapes_company_and_role(self, monkeypatch):
        captured = self._capture(monkeypatch)
        app_record = {
            "id": "app-1", "company": PAYLOAD, "role_title": PAYLOAD,
            "status_changed_at": "", "created_at": "",
        }
        api._researching_nudge_email("user@example.com", "user-1", app_record, tier=1)
        assert PAYLOAD not in captured["html"]
        assert ESCAPED in captured["html"]

    def test_follow_up_reminder_escapes_company_and_role(self, monkeypatch):
        captured = self._capture(monkeypatch)
        app_record = {
            "id": "app-1", "company": PAYLOAD, "role_title": PAYLOAD,
            "date_applied": "", "status_changed_at": "",
        }
        api._follow_up_reminder_email("user@example.com", "user-1", app_record, tier=1)
        assert PAYLOAD not in captured["html"]
        assert ESCAPED in captured["html"]

    def test_gone_silent_escapes_company_role_and_status(self, monkeypatch):
        captured = self._capture(monkeypatch)
        app_record = {
            "id": "app-1", "company": PAYLOAD, "role_title": PAYLOAD,
            "status": PAYLOAD, "status_changed_at": "", "updated_at": "",
        }
        api._gone_silent_email("user@example.com", "user-1", app_record)
        assert PAYLOAD not in captured["html"]
        assert ESCAPED in captured["html"]


class TestDigestEmailsEscaping:
    def _capture(self, monkeypatch):
        captured = {}

        def fake_send_email(to, subject, body, html=None):
            captured["html"] = html
            return True

        monkeypatch.setattr(api, "_send_email", fake_send_email)
        return captured

    def test_daily_digest_escapes_company_and_role(self, monkeypatch):
        captured = self._capture(monkeypatch)
        apps = [{"company": PAYLOAD, "role_title": PAYLOAD, "status": "Researching"}]
        api._daily_digest_email("user@example.com", "user-1", apps)
        assert PAYLOAD not in captured["html"]
        assert ESCAPED in captured["html"]

    def test_weekly_digest_escapes_company_and_role_in_silent_section(self, monkeypatch):
        captured = self._capture(monkeypatch)
        apps = [{
            "company": PAYLOAD, "role_title": PAYLOAD, "status": "Applied",
            "status_changed_at": "2020-01-01T00:00:00Z", "updated_at": "2020-01-01T00:00:00Z",
        }]
        api._weekly_digest_email("user@example.com", "user-1", apps)
        assert PAYLOAD not in captured["html"]
        assert ESCAPED in captured["html"]


class TestNotifDispatchNotificationsEscaping:
    def _capture(self, monkeypatch):
        captured = {}

        def fake_send_email(to, subject, body, html=None):
            captured["html"] = html
            return True

        monkeypatch.setattr(notif_dispatch, "send_email", fake_send_email)
        monkeypatch.setattr(notif_dispatch, "_get_user_email", lambda user_id: "user@example.com")
        monkeypatch.setattr(notif_dispatch, "_get_prefs", lambda user_id: {
            "new_application": True, "status_changed": True,
        })
        return captured

    def test_new_application_escapes_company_role_and_location(self, monkeypatch):
        captured = self._capture(monkeypatch)
        record = {
            "id": "app-1", "company": PAYLOAD, "role_title": PAYLOAD,
            "status": "Researching", "location": PAYLOAD,
        }
        notif_dispatch.notify_new_application("user-1", record)
        assert PAYLOAD not in captured["html"]
        assert ESCAPED in captured["html"]

    def test_status_changed_escapes_company_and_role(self, monkeypatch):
        captured = self._capture(monkeypatch)
        record = {"id": "app-1", "company": PAYLOAD, "role_title": PAYLOAD}
        notif_dispatch.notify_status_changed("user-1", record, "Researching", "Applied")
        assert PAYLOAD not in captured["html"]
        assert ESCAPED in captured["html"]
