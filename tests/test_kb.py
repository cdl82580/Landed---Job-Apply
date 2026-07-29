"""Tests for /api/admin/kb/seed-from-file — the KB extraction endpoint.

Historical note: this endpoint used to shell out to Node and eval() the
`const KB = {...}` literal out of frontend/kb.html. That had two problems:
eval() would run arbitrary code if the file ever came from anywhere less
trusted than this endpoint's own admin-only callers, and (regression from
79cfe0e) embedding the whole file in a `node -e` argument could hit the OS's
exec() argument-length limit (E2BIG) as kb.html grew, surfacing as an
uncaught OSError that bypassed the endpoint's HTTPException handling.

It now reads kb.html directly and parses the literal with
scripts/js_object_parser — a small parser that only ever produces plain data
or raises, never executes anything, so both failure modes above are gone
structurally rather than papered over.
"""

from unittest.mock import mock_open, patch


class TestSeedFromFileAuth:
    def test_requires_auth(self, client):
        r = client.post("/api/admin/kb/seed-from-file")
        assert r.status_code == 401

    def test_requires_admin(self, authed_client):
        r = authed_client.post("/api/admin/kb/seed-from-file")
        assert r.status_code == 403


class TestSeedFromFileRealExtraction:
    """Runs the real parser against the real frontend/kb.html — the most
    direct proof it keeps working as kb.html keeps growing."""

    def test_extracts_categories_and_articles_without_crashing(self, admin_client):
        r = admin_client.post("/api/admin/kb/seed-from-file")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        assert data["categories"] > 0
        assert data["articles"] > 0


class TestSeedFromFileErrorHandling:
    def test_missing_file_returns_500(self, admin_client):
        with patch("os.path.exists", return_value=False):
            r = admin_client.post("/api/admin/kb/seed-from-file")
        assert r.status_code == 500
        assert "not found" in r.json()["detail"].lower()

    def test_kb_const_not_found_returns_json_500_not_a_raw_crash(self, admin_client):
        """The old bug's failure mode (an uncaught error bypassing the
        endpoint's HTTPException handling) is checked for generally here:
        whatever goes wrong, the response must stay a normal JSON error."""
        with patch("builtins.open", mock_open(read_data="<html>no KB literal here</html>")):
            r = admin_client.post("/api/admin/kb/seed-from-file")
        assert r.status_code == 500
        body = r.json()
        assert "detail" in body
        assert "KB const not found" in body["detail"]

    def test_malformed_literal_returns_500_with_parse_error(self, admin_client):
        """A syntactically invalid value (here, a bare function call) must be
        rejected as a parse error — never silently accepted or, worse,
        executed the way eval() would have."""
        fake_html = "const KB = { articles: [ (function(){})() ] }"
        with patch("builtins.open", mock_open(read_data=fake_html)):
            r = admin_client.post("/api/admin/kb/seed-from-file")
        assert r.status_code == 500
        assert "KB parsing failed" in r.json()["detail"]

    def test_no_subprocess_or_eval_involved(self, admin_client):
        """The fix's core contract: extraction must not shell out at all —
        removing the subprocess call removes the whole class of argv-limit
        and code-execution bugs the old implementation was exposed to."""
        with patch("subprocess.run") as mock_run:
            r = admin_client.post("/api/admin/kb/seed-from-file")
        assert r.status_code == 200, r.text
        mock_run.assert_not_called()
