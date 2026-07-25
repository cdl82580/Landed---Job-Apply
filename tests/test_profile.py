"""Integration tests for /api/profile endpoints."""

import io
import pytest
from tests.conftest import _store


class TestGetProfile:
    def test_returns_profile_fields(self, authed_client, user_record):
        r = authed_client.get("/api/profile")
        assert r.status_code == 200
        d = r.json()
        assert d["email"] == user_record["email"]
        assert "display_name" in d
        assert "profile_text" in d
        assert "has_resume" in d

    def test_profile_text_empty_when_unset(self, authed_client):
        r = authed_client.get("/api/profile")
        assert r.json()["profile_text"] == ""

    def test_profile_text_present_when_saved(self, authed_client, user_record):
        _store.save_profile(user_record["user_id"], "# My voice guide")
        r = authed_client.get("/api/profile")
        assert "My voice guide" in r.json()["profile_text"]

    def test_has_resume_false_when_none(self, authed_client):
        r = authed_client.get("/api/profile")
        assert r.json()["has_resume"] is False

    def test_has_resume_true_when_uploaded(self, authed_client, user_record):
        _store.save_resume(user_record["user_id"], b"fake docx bytes")
        r = authed_client.get("/api/profile")
        assert r.json()["has_resume"] is True


class TestUpdateProfile:
    def test_update_display_name(self, authed_client):
        r = authed_client.put("/api/profile", json={"display_name": "New Name"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # Verify it persisted
        r2 = authed_client.get("/api/profile")
        assert r2.json()["display_name"] == "New Name"

    def test_update_profile_text(self, authed_client, user_record):
        r = authed_client.put("/api/profile", json={"profile_text": "# Hello\n\nWorld"})
        assert r.status_code == 200
        saved = _store.get_profile(user_record["user_id"])
        assert "Hello" in saved

    def test_display_name_too_long(self, authed_client):
        r = authed_client.put("/api/profile", json={"display_name": "x" * 300})
        assert r.status_code == 400

    def test_profile_text_too_long(self, authed_client):
        r = authed_client.put("/api/profile", json={"profile_text": "x" * 200_001})
        assert r.status_code == 400

    def test_empty_body_is_ok(self, authed_client):
        r = authed_client.put("/api/profile", json={})
        assert r.status_code == 200

    def test_unauthenticated(self, client):
        r = client.put("/api/profile", json={"display_name": "X"})
        assert r.status_code == 401


class TestResumeUpload:
    def test_upload_docx(self, authed_client, user_record):
        # API requires >= 1000 bytes and .docx extension
        fake_docx = b"PK\x03\x04" + b"\x00" * 2000
        r = authed_client.post(
            "/api/profile/resume",
            files={"resume": ("master.docx", io.BytesIO(fake_docx), "application/octet-stream")},
        )
        assert r.status_code == 200
        assert _store.has_resume(user_record["user_id"])

    def test_rejects_non_docx(self, authed_client):
        r = authed_client.post(
            "/api/profile/resume",
            files={"resume": ("resume.pdf", io.BytesIO(b"%PDF"), "application/pdf")},
        )
        assert r.status_code == 400

    def test_rejects_non_zip_docx(self, authed_client):
        """A .docx that isn't actually a ZIP archive should be rejected."""
        not_a_zip = b"NOT_A_ZIP" + b"\x00" * 2000
        r = authed_client.post(
            "/api/profile/resume",
            files={"resume": ("resume.docx", io.BytesIO(not_a_zip), "application/octet-stream")},
        )
        assert r.status_code == 400
        assert "zip" in r.json()["detail"].lower() or "valid" in r.json()["detail"].lower()

    def test_rejects_too_small_file(self, authed_client):
        small = b"PK\x03\x04" + b"\x00" * 10
        r = authed_client.post(
            "/api/profile/resume",
            files={"resume": ("resume.docx", io.BytesIO(small), "application/octet-stream")},
        )
        assert r.status_code == 400

    def test_unauthenticated(self, client):
        r = client.post(
            "/api/profile/resume",
            files={"resume": ("r.docx", io.BytesIO(b"x"), "application/octet-stream")},
        )
        assert r.status_code == 401

    def test_first_upload_has_no_previous(self, authed_client, user_record):
        fake_docx = b"PK\x03\x04" + b"\x00" * 2000
        authed_client.post(
            "/api/profile/resume",
            files={"resume": ("master.docx", io.BytesIO(fake_docx), "application/octet-stream")},
        )
        assert not _store.has_previous_resume(user_record["user_id"])

    def test_second_upload_creates_previous(self, authed_client, user_record):
        first  = b"PK\x03\x04" + b"\x01" * 2000
        second = b"PK\x03\x04" + b"\x02" * 2000
        authed_client.post("/api/profile/resume",
            files={"resume": ("a.docx", io.BytesIO(first), "application/octet-stream")})
        r = authed_client.post("/api/profile/resume",
            files={"resume": ("b.docx", io.BytesIO(second), "application/octet-stream")})
        assert r.status_code == 200
        assert _store.has_previous_resume(user_record["user_id"])
        assert _store.get_previous_resume(user_record["user_id"]) == first
        assert _store.get_resume(user_record["user_id"]) == second

        profile = authed_client.get("/api/profile").json()
        assert profile["has_previous_resume"] is True
        assert profile["resume_previous_saved_at"] is not None


class TestPreviousResume:
    def _upload(self, client, data, filename="master.docx"):
        return client.post("/api/profile/resume",
            files={"resume": (filename, io.BytesIO(data), "application/octet-stream")})

    def test_download_requires_auth(self, client):
        r = client.get("/api/profile/resume/previous")
        assert r.status_code == 401

    def test_download_404_when_none(self, authed_client):
        r = authed_client.get("/api/profile/resume/previous")
        assert r.status_code == 404

    def test_download_returns_previous_bytes(self, authed_client):
        first  = b"PK\x03\x04" + b"\x01" * 2000
        second = b"PK\x03\x04" + b"\x02" * 2000
        self._upload(authed_client, first)
        self._upload(authed_client, second)
        r = authed_client.get("/api/profile/resume/previous")
        assert r.status_code == 200
        assert r.content == first
        assert "attachment" in r.headers["content-disposition"]
        assert "master_previous.docx" in r.headers["content-disposition"]

    def test_restore_requires_auth(self, client):
        r = client.post("/api/profile/resume/restore")
        assert r.status_code == 401

    def test_restore_404_when_none(self, authed_client):
        r = authed_client.post("/api/profile/resume/restore")
        assert r.status_code == 404

    def test_restore_swaps_current_and_previous(self, authed_client, user_record):
        first  = b"PK\x03\x04" + b"\x01" * 2000
        second = b"PK\x03\x04" + b"\x02" * 2000
        self._upload(authed_client, first)
        self._upload(authed_client, second)
        r = authed_client.post("/api/profile/resume/restore")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert _store.get_resume(user_record["user_id"]) == first
        assert _store.get_previous_resume(user_record["user_id"]) == second

    def test_restore_updates_profile_timestamps(self, authed_client):
        first  = b"PK\x03\x04" + b"\x01" * 2000
        second = b"PK\x03\x04" + b"\x02" * 2000
        self._upload(authed_client, first)
        self._upload(authed_client, second)
        authed_client.post("/api/profile/resume/restore")
        after = authed_client.get("/api/profile").json()
        # Timestamps have 1s resolution, so don't assert they differ from
        # pre-restore — just that restore stamped both fields, and the
        # backup slot is still populated (restore is a swap, not a delete).
        assert after["resume_uploaded_at"] is not None
        assert after["resume_previous_saved_at"] is not None
        assert after["has_previous_resume"] is True
