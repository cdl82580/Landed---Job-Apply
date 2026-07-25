"""
Shared fixtures for the job-apply test suite.

Strategy
--------
All tests run fully offline — no real S3, no real Claude API, no real Drive.
Storage is swapped out for an in-memory dict backend.
The FastAPI TestClient is used for all HTTP tests (no live server needed).
"""

import hashlib
import json
import os
import time
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ── Environment stubs (must be set before api.py is imported) ─────────────────
os.environ.setdefault("SESSION_SECRET", "test-secret-do-not-use-in-prod")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-key")
os.environ.setdefault("BUCKET_NAME", "test-bucket")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test-key")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test-secret")
os.environ.setdefault("AWS_ENDPOINT_URL_S3", "https://test.storage.example.com")
os.environ.setdefault("APP_URL", "http://testserver")

# ── In-memory storage backend ─────────────────────────────────────────────────

class MemoryStore:
    """Thread-safe in-memory replacement for scripts.storage (Tigris S3)."""

    def __init__(self):
        self._data: dict[str, bytes] = {}

    def put_bytes(self, key: str, data: bytes, content_type: str = "") -> None:
        self._data[key] = data

    def delete_bytes(self, key: str) -> None:
        self._data.pop(key, None)

    def put_text(self, key: str, text: str) -> None:
        self._data[key] = text.encode()

    def delete_text(self, key: str) -> None:
        self._data.pop(key, None)

    def get_bytes(self, key: str) -> bytes | None:
        return self._data.get(key)

    def get_text(self, key: str) -> str | None:
        v = self._data.get(key)
        return v.decode() if v is not None else None

    def exists(self, key: str) -> bool:
        return key in self._data

    def list_keys(self, prefix: str) -> list[str]:
        return [k for k in self._data if k.startswith(prefix)]

    def is_configured(self) -> bool:
        return True

    # ── User helpers (mirrors storage.py public API) ──────────────────────────

    def _email_key(self, email: str) -> str:
        return hashlib.sha256(email.strip().lower().encode()).hexdigest()

    def get_user_by_email(self, email: str) -> dict | None:
        raw = self.get_text(f"users/{self._email_key(email)}.json")
        return json.loads(raw) if raw else None

    def get_user_by_id(self, user_id: str) -> dict | None:
        email = self.get_text(f"user_ids/{user_id}.txt")
        return self.get_user_by_email(email.strip()) if email else None

    def save_user(self, user: dict) -> None:
        email = user["email"].strip().lower()
        self.put_text(f"users/{self._email_key(email)}.json", json.dumps(user))
        self.put_text(f"user_ids/{user['user_id']}.txt", email)

    def list_all_users(self) -> list[dict]:
        results = []
        for key in self.list_keys("user_ids/"):
            user_id = key.split("/", 1)[1].removesuffix(".txt")
            u = self.get_user_by_id(user_id)
            if u:
                results.append(u)
        return results

    def get_user_by_google_id(self, google_id: str) -> dict | None:
        for u in self.list_all_users():
            if u.get("google_id") == google_id:
                return u
        return None

    def update_user_email(self, user: dict, new_email: str) -> None:
        old_email = user["email"].strip().lower()
        self.delete_bytes(f"users/{self._email_key(old_email)}.json")
        user["email"] = new_email.strip().lower()
        self.save_user(user)

    def save_resume(self, user_id: str, data: bytes) -> bool:
        existing = self.get_bytes(f"resumes/{user_id}/master.docx")
        backed_up = existing is not None
        if backed_up:
            self.put_bytes(f"resumes/{user_id}/master_previous.docx", existing)
        self.put_bytes(f"resumes/{user_id}/master.docx", data)
        return backed_up

    def get_resume(self, user_id: str) -> bytes | None:
        return self.get_bytes(f"resumes/{user_id}/master.docx")

    def has_resume(self, user_id: str) -> bool:
        return self.exists(f"resumes/{user_id}/master.docx")

    def get_previous_resume(self, user_id: str) -> bytes | None:
        return self.get_bytes(f"resumes/{user_id}/master_previous.docx")

    def has_previous_resume(self, user_id: str) -> bool:
        return self.exists(f"resumes/{user_id}/master_previous.docx")

    def restore_previous_resume(self, user_id: str) -> bool:
        previous = self.get_bytes(f"resumes/{user_id}/master_previous.docx")
        if previous is None:
            return False
        self.save_resume(user_id, previous)
        return True

    def save_profile(self, user_id: str, text: str) -> None:
        self.put_text(f"profiles/{user_id}/profile.md", text)

    def get_profile(self, user_id: str) -> str | None:
        return self.get_text(f"profiles/{user_id}/profile.md")


_store = MemoryStore()


@pytest.fixture(autouse=True)
def reset_store():
    """Wipe in-memory store before each test."""
    _store._data.clear()
    yield
    _store._data.clear()


@pytest.fixture(scope="session")
def memory_store():
    return _store


# ── Patch storage globally before importing api ───────────────────────────────

import scripts.storage as _real_storage  # noqa: E402

@pytest.fixture(autouse=True)
def patch_storage(monkeypatch):
    """Replace every storage function with the MemoryStore equivalent."""
    for attr in dir(_store):
        if not attr.startswith("_"):
            monkeypatch.setattr(_real_storage, attr, getattr(_store, attr))
    yield


@pytest.fixture(autouse=True)
def reset_rate_limits():
    """Clear api._rl_buckets before each test.

    Rate-limit state is a plain module-level dict (api.py:_rl_buckets), not
    otherwise test-isolated — without this, tests that call a rate-limited
    endpoint (e.g. /api/profile/resume, capped at 10/hr) enough times across
    a test file can trip the real limit and get a silent 429 instead of the
    200 the test expects, since the same client IP is reused for every
    TestClient request.
    """
    import api as _api
    _api._rl_buckets.clear()
    yield
    _api._rl_buckets.clear()


# ── FastAPI TestClient ─────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def app():
    import api as _api
    return _api.app


@pytest.fixture()
def client(app, patch_storage):
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_user(
    email: str = "test@example.com",
    password: str = "password123",
    role: str = "user",
    display_name: str = "Test User",
) -> dict[str, Any]:
    """Create and persist a user record with a valid scrypt hash."""
    import hashlib, hmac, os, base64
    # Use a simple but valid scrypt hash format that scripts.session can verify
    from scripts import session as _sess
    salt = os.urandom(16).hex()
    # Use werkzeug-style placeholder — tests don't need real password verification
    # Just store something that won't be None
    user_id = str(uuid.uuid4())
    record = {
        "user_id": user_id,
        "email": email.strip().lower(),
        "display_name": display_name,
        "password_hash": f"scrypt:{salt}:placeholder",
        "role": role,
        "email_verified": True,
        "active": True,
        "created_at": "2026-01-01T00:00:00Z",
    }
    _store.save_user(record)
    return record


def login(client: TestClient, email: str, password: str = "password123") -> str:
    """Return session cookie value after login. Caller must ensure user exists."""
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.cookies.get("session", "")


@pytest.fixture()
def user_record():
    return make_user()


@pytest.fixture()
def admin_record():
    return make_user(email="admin@example.com", role="admin", display_name="Admin")


@pytest.fixture()
def authed_client(client, user_record, monkeypatch):
    """Client with a valid session already established (bypasses password check)."""
    import api as _api
    # Patch _verify_session to return our user directly
    token = _api._create_session(
        user_record["user_id"], user_record["email"], role="user",
        password_hash=user_record["password_hash"],
    )
    client.cookies.set("session", token)
    return client


@pytest.fixture()
def admin_client(client, admin_record, monkeypatch):
    """Client with an admin session."""
    import api as _api
    token = _api._create_session(
        admin_record["user_id"], admin_record["email"], role="admin",
        password_hash=admin_record["password_hash"],
    )
    client.cookies.set("session", token)
    return client
