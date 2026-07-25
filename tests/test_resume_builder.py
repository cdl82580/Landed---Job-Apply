"""Tests for the Master Resume Builder agent: /api/resume-builder endpoints,
scripts/resume_builder.py's polish/render steps, and the background worker."""

import json
import shutil
import uuid
from pathlib import Path
from queue import Queue
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import _store

VALID_BODY = {
    "template": "technical",
    "full_name": "Jane Doe",
    "email": "jane@example.com",
    "jobs": [
        {"company": "Acme", "title": "Engineer", "bullets": ["Built things."]},
    ],
}


# ---------------------------------------------------------------------------
# POST /api/resume-builder — request validation & gating
# ---------------------------------------------------------------------------

class TestCreateResumeBuilder:
    def test_requires_auth(self, client):
        r = client.post("/api/resume-builder", json=VALID_BODY)
        assert r.status_code == 401

    def test_admin_blocked(self, admin_client):
        r = admin_client.post("/api/resume-builder", json=VALID_BODY)
        assert r.status_code == 403

    def test_missing_template_rejected(self, authed_client):
        body = {k: v for k, v in VALID_BODY.items() if k != "template"}
        r = authed_client.post("/api/resume-builder", json=body)
        assert r.status_code == 422

    def test_unknown_template_rejected(self, authed_client):
        body = {**VALID_BODY, "template": "bogus"}
        r = authed_client.post("/api/resume-builder", json=body)
        assert r.status_code == 400

    def test_missing_name_rejected(self, authed_client):
        body = {**VALID_BODY, "full_name": "   "}
        r = authed_client.post("/api/resume-builder", json=body)
        assert r.status_code == 400

    def test_missing_email_rejected(self, authed_client):
        body = {**VALID_BODY, "email": ""}
        r = authed_client.post("/api/resume-builder", json=body)
        assert r.status_code == 400

    def test_requires_job_or_education_content(self, authed_client):
        body = {**VALID_BODY, "jobs": []}
        r = authed_client.post("/api/resume-builder", json=body)
        assert r.status_code == 400

    def test_job_without_bullets_is_insufficient(self, authed_client):
        body = {**VALID_BODY, "jobs": [{"company": "Acme", "title": "Engineer", "bullets": []}]}
        r = authed_client.post("/api/resume-builder", json=body)
        assert r.status_code == 400

    def test_education_alone_is_sufficient(self, authed_client):
        body = {**VALID_BODY, "jobs": [],
                "education": [{"degree": "B.S. CS", "institution": "State University"}]}
        with patch("api.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            r = authed_client.post("/api/resume-builder", json=body)
        assert r.status_code == 200

    def test_too_many_jobs_rejected(self, authed_client):
        job = {"company": "Acme", "title": "Engineer", "bullets": ["Did stuff."]}
        body = {**VALID_BODY, "jobs": [job] * 20}
        r = authed_client.post("/api/resume-builder", json=body)
        assert r.status_code == 400

    def test_too_many_bullets_on_one_job_rejected(self, authed_client):
        job = {"company": "Acme", "title": "Engineer", "bullets": ["x"] * 20}
        body = {**VALID_BODY, "jobs": [job]}
        r = authed_client.post("/api/resume-builder", json=body)
        assert r.status_code == 400

    def test_returns_run_id_and_machine_id(self, authed_client):
        with patch("api.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            r = authed_client.post("/api/resume-builder", json=VALID_BODY)
        assert r.status_code == 200
        d = r.json()
        assert "run_id" in d
        assert uuid.UUID(d["run_id"])
        assert "machine_id" in d

    def test_run_id_in_store_and_status_pollable(self, authed_client):
        with patch("api.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            r = authed_client.post("/api/resume-builder", json=VALID_BODY)
        run_id = r.json()["run_id"]
        r2 = authed_client.get(f"/api/resume-builder/{run_id}/status")
        assert r2.status_code == 200
        assert r2.json()["run_id"] == run_id

    def test_status_unknown_run(self, authed_client):
        r = authed_client.get(f"/api/resume-builder/{uuid.uuid4()}/status")
        assert r.status_code == 404

    def test_stream_requires_auth(self, client):
        r = client.get(f"/api/resume-builder/{uuid.uuid4()}/stream")
        assert r.status_code == 401

    def test_stream_unknown_run(self, authed_client):
        r = authed_client.get(f"/api/resume-builder/{uuid.uuid4()}/stream")
        assert r.status_code == 404

    def test_files_not_complete(self, authed_client):
        with patch("api.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            r = authed_client.post("/api/resume-builder", json=VALID_BODY)
        run_id = r.json()["run_id"]
        r2 = authed_client.get(f"/api/resume-builder/{run_id}/files/master.docx")
        assert r2.status_code == 404


class TestOverwriteConfirmation:
    def test_409_when_resume_already_exists(self, authed_client, user_record):
        _store.save_resume(user_record["user_id"], b"existing docx bytes")
        r = authed_client.post("/api/resume-builder", json=VALID_BODY)
        assert r.status_code == 409

    def test_confirm_overwrite_proceeds(self, authed_client, user_record):
        _store.save_resume(user_record["user_id"], b"existing docx bytes")
        body = {**VALID_BODY, "confirm_overwrite": True}
        with patch("api.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            r = authed_client.post("/api/resume-builder", json=body)
        assert r.status_code == 200

    def test_no_existing_resume_does_not_need_confirmation(self, authed_client):
        with patch("api.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            r = authed_client.post("/api/resume-builder", json=VALID_BODY)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Worker success/failure paths — render_resume's node subprocess mocked out,
# matching how little the rest of this suite exercises the Node-docx codepath.
# ---------------------------------------------------------------------------

class TestResumeBuilderWorker:
    def _make_entry(self, api, user_id):
        entry_id = f"test-{uuid.uuid4()}"
        api._resume_builders[entry_id] = {
            "queue": Queue(), "status": "queued", "result": None, "error": None,
            "user_id": user_id,
        }
        return entry_id

    def _drain(self, q):
        msgs = []
        while True:
            m = q.get_nowait()
            msgs.append(m)
            if m is None:
                break
        return msgs

    def test_success_path_saves_resume_and_completes(self, user_record, monkeypatch):
        import api
        from scripts import resume_builder

        def fake_render(data, template, output_path, progress=print):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"PK\x03\x04fake docx bytes")

        monkeypatch.setattr(resume_builder, "render_resume", fake_render)

        req = api.ResumeBuilderRequest(**VALID_BODY, polish=False)
        entry_id = self._make_entry(api, user_record["user_id"])

        api._resume_builder_worker(
            api._resume_builders, entry_id, user_record["user_id"], user_record["email"], req)

        entry = api._resume_builders[entry_id]
        assert entry["status"] == "done"
        assert _store.has_resume(user_record["user_id"])
        assert _store.get_resume(user_record["user_id"]) == b"PK\x03\x04fake docx bytes"

        msgs = self._drain(entry["queue"])
        assert msgs[-1] is None
        done_msgs = [m for m in msgs if m and m.get("type") == "done"]
        assert len(done_msgs) == 1
        assert done_msgs[0]["files"]["resume"] == "master.docx"

    def test_second_run_backs_up_first(self, user_record, monkeypatch):
        import api
        from scripts import resume_builder

        outputs = iter([b"PK\x03\x04first render", b"PK\x03\x04second render"])

        def fake_render(data, template, output_path, progress=print):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(next(outputs))

        monkeypatch.setattr(resume_builder, "render_resume", fake_render)

        req = api.ResumeBuilderRequest(**VALID_BODY, polish=False, confirm_overwrite=True)

        entry_id_1 = self._make_entry(api, user_record["user_id"])
        api._resume_builder_worker(
            api._resume_builders, entry_id_1, user_record["user_id"], user_record["email"], req)
        assert not _store.has_previous_resume(user_record["user_id"])
        assert _store.get_resume(user_record["user_id"]) == b"PK\x03\x04first render"

        entry_id_2 = self._make_entry(api, user_record["user_id"])
        api._resume_builder_worker(
            api._resume_builders, entry_id_2, user_record["user_id"], user_record["email"], req)
        assert _store.has_previous_resume(user_record["user_id"])
        assert _store.get_previous_resume(user_record["user_id"]) == b"PK\x03\x04first render"
        assert _store.get_resume(user_record["user_id"]) == b"PK\x03\x04second render"

    def test_polish_called_when_enabled(self, user_record, monkeypatch):
        import api
        from scripts import resume_builder

        def fake_render(data, template, output_path, progress=print):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"PK\x03\x04fake docx bytes")

        polish_calls = []

        def fake_polish(data, model, progress):
            polish_calls.append(data)
            return data

        monkeypatch.setattr(resume_builder, "render_resume", fake_render)
        monkeypatch.setattr(resume_builder, "polish_resume_data", fake_polish)

        req = api.ResumeBuilderRequest(**VALID_BODY, polish=True)
        entry_id = self._make_entry(api, user_record["user_id"])
        api._resume_builder_worker(
            api._resume_builders, entry_id, user_record["user_id"], user_record["email"], req)

        assert len(polish_calls) == 1
        assert api._resume_builders[entry_id]["status"] == "done"

    def test_polish_skipped_when_disabled(self, user_record, monkeypatch):
        import api
        from scripts import resume_builder

        def fake_render(data, template, output_path, progress=print):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"PK\x03\x04fake docx bytes")

        polish_calls = []
        monkeypatch.setattr(resume_builder, "render_resume", fake_render)
        monkeypatch.setattr(resume_builder, "polish_resume_data",
                            lambda *a, **k: polish_calls.append(1) or a[0])

        req = api.ResumeBuilderRequest(**VALID_BODY, polish=False)
        entry_id = self._make_entry(api, user_record["user_id"])
        api._resume_builder_worker(
            api._resume_builders, entry_id, user_record["user_id"], user_record["email"], req)

        assert polish_calls == []

    def test_render_failure_marks_error_and_does_not_save_resume(self, user_record, monkeypatch):
        import api
        from apply import WorkflowError
        from scripts import resume_builder

        def failing_render(data, template, output_path, progress=print):
            raise WorkflowError("Resume generation JS failed: boom")

        monkeypatch.setattr(resume_builder, "render_resume", failing_render)

        req = api.ResumeBuilderRequest(**VALID_BODY, polish=False)
        entry_id = self._make_entry(api, user_record["user_id"])
        api._resume_builder_worker(
            api._resume_builders, entry_id, user_record["user_id"], user_record["email"], req)

        entry = api._resume_builders[entry_id]
        assert entry["status"] == "error"
        assert "boom" in entry["error"]
        assert not _store.has_resume(user_record["user_id"])

        msgs = self._drain(entry["queue"])
        assert any(m and m.get("type") == "error" for m in msgs)


# ---------------------------------------------------------------------------
# scripts/resume_builder.py — polish step (mocked Claude call)
# ---------------------------------------------------------------------------

class TestPolishResumeData:
    def test_returns_polished_data_on_success(self, monkeypatch):
        from scripts import resume_builder

        polished = {**VALID_BODY, "summary": "Polished summary."}
        monkeypatch.setattr(resume_builder, "claude", lambda **kw: json.dumps(polished))

        result = resume_builder.polish_resume_data(VALID_BODY, "claude-sonnet-5", print)
        assert result["summary"] == "Polished summary."

    def test_falls_back_to_original_on_bad_json(self, monkeypatch):
        from scripts import resume_builder

        monkeypatch.setattr(resume_builder, "claude", lambda **kw: "not valid json {{{")

        result = resume_builder.polish_resume_data(VALID_BODY, "claude-sonnet-5", print)
        assert result == VALID_BODY

    def test_falls_back_to_original_on_missing_jobs_key(self, monkeypatch):
        from scripts import resume_builder

        monkeypatch.setattr(resume_builder, "claude", lambda **kw: json.dumps({"summary": "x"}))

        result = resume_builder.polish_resume_data(VALID_BODY, "claude-sonnet-5", print)
        assert result == VALID_BODY

    def test_falls_back_to_original_on_exception(self, monkeypatch):
        from scripts import resume_builder

        def boom(**kw):
            raise RuntimeError("API down")

        monkeypatch.setattr(resume_builder, "claude", boom)

        result = resume_builder.polish_resume_data(VALID_BODY, "claude-sonnet-5", print)
        assert result == VALID_BODY


# ---------------------------------------------------------------------------
# scripts/resume_builder.py — render step (real `node` subprocess).
# Writes under output/ (not pytest's tmp_path) since the generated script's
# require('docx') must resolve node_modules by walking up from its own
# directory — see scripts/resume_builder.py:render_resume.
# ---------------------------------------------------------------------------

class TestRenderResume:
    @pytest.fixture()
    def scratch_dir(self):
        d = Path("output") / f"_test_resume_builder_{uuid.uuid4().hex}"
        yield d
        shutil.rmtree(d, ignore_errors=True)

    def test_unknown_template_raises(self, scratch_dir):
        from apply import WorkflowError
        from scripts import resume_builder

        with pytest.raises(WorkflowError):
            resume_builder.render_resume(VALID_BODY, "not-a-template", scratch_dir / "master.docx")

    def test_renders_valid_docx(self, scratch_dir):
        from scripts import resume_builder

        out = scratch_dir / "master.docx"
        resume_builder.render_resume(VALID_BODY, "technical", out, progress=lambda m: None)
        assert out.exists()
        assert out.read_bytes()[:4] == b"PK\x03\x04"
