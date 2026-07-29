"""Tests for /api/notifications/action — GET must only render a confirm
page (no side effects), POST must be the one that actually executes the
action. Regression coverage for the fix that stopped GET from mutating
state directly (email-security scanners prefetch GET links automatically,
which would otherwise silently trigger the action before the user ever
opened the email)."""

from scripts.notification_tokens import create_token


def _create_application(client, **overrides):
    body = {"company": "Acme", "role_title": "Engineer", **overrides}
    r = client.post("/api/applications", json=body)
    assert r.status_code == 201, r.text
    return r.json()


class TestNotificationActionConfirmFlow:
    def test_get_renders_confirm_page_without_executing(self, authed_client, user_record):
        app = _create_application(authed_client)
        token = create_token(
            user_record["user_id"], app["id"], "status",
            {"status": "Interviewing"},
        )

        r = authed_client.get(f"/api/notifications/action?token={token}", follow_redirects=False)
        assert r.status_code == 200
        assert "Confirm" in r.text
        assert 'action="/api/notifications/action"' in r.text
        assert 'method="POST"' in r.text

        # No side effect yet — status must be unchanged.
        current = authed_client.get(f"/api/applications/{app['id']}").json()
        assert current["status"] == "Researching"

    def test_post_executes_the_action(self, authed_client, user_record):
        app = _create_application(authed_client)
        token = create_token(
            user_record["user_id"], app["id"], "status",
            {"status": "Interviewing"},
        )

        r = authed_client.post("/api/notifications/action", data={"token": token})
        assert r.status_code == 200
        assert "Status updated" in r.text

        current = authed_client.get(f"/api/applications/{app['id']}").json()
        assert current["status"] == "Interviewing"

    def test_invalid_token_rejected_on_get_and_post(self, authed_client):
        r = authed_client.get("/api/notifications/action?token=garbage")
        assert r.status_code == 400

        r = authed_client.post("/api/notifications/action", data={"token": "garbage"})
        assert r.status_code == 400

    def test_applied_without_date_redirects_straight_to_confirm_applied(self, authed_client, user_record):
        """This case already requires its own POST (the date-picker form),
        so GET can safely redirect there instead of showing a generic
        confirm page first."""
        app = _create_application(authed_client)
        token = create_token(
            user_record["user_id"], app["id"], "status",
            {"status": "Applied"},
        )

        r = authed_client.get(f"/api/notifications/action?token={token}", follow_redirects=False)
        assert r.status_code == 302
        assert "/api/notifications/confirm-applied" in r.headers["location"]
