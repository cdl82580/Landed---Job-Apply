"""Unit tests for the in-memory storage backend (mirrors scripts/storage.py API)."""

import json
import pytest
from tests.conftest import MemoryStore


@pytest.fixture()
def store():
    s = MemoryStore()
    return s


class TestBytesOps:
    def test_put_and_get(self, store):
        store.put_bytes("k/file.bin", b"\x00\x01\x02")
        assert store.get_bytes("k/file.bin") == b"\x00\x01\x02"

    def test_missing_key_returns_none(self, store):
        assert store.get_bytes("nonexistent") is None

    def test_delete(self, store):
        store.put_bytes("k", b"data")
        store.delete_bytes("k")
        assert store.get_bytes("k") is None

    def test_delete_nonexistent_is_safe(self, store):
        store.delete_bytes("does-not-exist")  # should not raise


class TestTextOps:
    def test_put_and_get(self, store):
        store.put_text("notes/a.txt", "hello world")
        assert store.get_text("notes/a.txt") == "hello world"

    def test_unicode(self, store):
        store.put_text("k", "em—dash")
        assert store.get_text("k") == "em—dash"

    def test_missing_returns_none(self, store):
        assert store.get_text("missing") is None

    def test_delete(self, store):
        store.put_text("k", "v")
        store.delete_text("k")
        assert store.get_text("k") is None


class TestExists:
    def test_exists_after_put(self, store):
        store.put_bytes("x", b"y")
        assert store.exists("x") is True

    def test_not_exists_before_put(self, store):
        assert store.exists("x") is False

    def test_not_exists_after_delete(self, store):
        store.put_bytes("x", b"y")
        store.delete_bytes("x")
        assert store.exists("x") is False


class TestListKeys:
    def test_prefix_filter(self, store):
        store.put_text("users/a", "a")
        store.put_text("users/b", "b")
        store.put_text("other/c", "c")
        keys = store.list_keys("users/")
        assert set(keys) == {"users/a", "users/b"}

    def test_empty_result(self, store):
        assert store.list_keys("nothing/") == []


class TestUserOps:
    def _user(self, email="alice@example.com"):
        import uuid
        return {
            "user_id": str(uuid.uuid4()),
            "email": email,
            "display_name": "Alice",
            "password_hash": "scrypt:salt:hash",
            "role": "user",
            "email_verified": True,
        }

    def test_save_and_get_by_email(self, store):
        u = self._user()
        store.save_user(u)
        result = store.get_user_by_email(u["email"])
        assert result["user_id"] == u["user_id"]

    def test_get_by_email_case_insensitive(self, store):
        u = self._user("Bob@Example.COM")
        store.save_user(u)
        assert store.get_user_by_email("bob@example.com") is not None

    def test_get_by_id(self, store):
        u = self._user()
        store.save_user(u)
        result = store.get_user_by_id(u["user_id"])
        assert result["email"] == u["email"].lower()

    def test_unknown_id_returns_none(self, store):
        assert store.get_user_by_id("bad-id") is None

    def test_list_all_users(self, store):
        store.save_user(self._user("a@b.com"))
        store.save_user(self._user("c@d.com"))
        users = store.list_all_users()
        emails = [u["email"] for u in users]
        assert "a@b.com" in emails
        assert "c@d.com" in emails

    def test_update_email(self, store):
        u = self._user("old@example.com")
        store.save_user(u)
        store.update_user_email(u, "new@example.com")
        assert store.get_user_by_email("old@example.com") is None
        assert store.get_user_by_email("new@example.com") is not None

    def test_overwrite_user(self, store):
        u = self._user()
        store.save_user(u)
        u["display_name"] = "Updated"
        store.save_user(u)
        result = store.get_user_by_email(u["email"])
        assert result["display_name"] == "Updated"


class TestResumeOps:
    def test_save_and_get(self, store):
        store.save_resume("uid-1", b"docx bytes")
        assert store.get_resume("uid-1") == b"docx bytes"

    def test_has_resume_true(self, store):
        store.save_resume("uid-1", b"x")
        assert store.has_resume("uid-1") is True

    def test_has_resume_false(self, store):
        assert store.has_resume("uid-nobody") is False

    def test_first_save_returns_false_and_no_backup(self, store):
        backed_up = store.save_resume("uid-1", b"v1")
        assert backed_up is False
        assert store.has_previous_resume("uid-1") is False
        assert store.get_previous_resume("uid-1") is None

    def test_second_save_returns_true_and_backs_up_first(self, store):
        store.save_resume("uid-1", b"v1")
        backed_up = store.save_resume("uid-1", b"v2")
        assert backed_up is True
        assert store.has_previous_resume("uid-1") is True
        assert store.get_previous_resume("uid-1") == b"v1"
        assert store.get_resume("uid-1") == b"v2"

    def test_restore_swaps_current_and_backup(self, store):
        store.save_resume("uid-1", b"v1")
        store.save_resume("uid-1", b"v2")
        restored = store.restore_previous_resume("uid-1")
        assert restored is True
        assert store.get_resume("uid-1") == b"v1"
        assert store.get_previous_resume("uid-1") == b"v2"

    def test_restore_twice_returns_to_original(self, store):
        store.save_resume("uid-1", b"v1")
        store.save_resume("uid-1", b"v2")
        store.restore_previous_resume("uid-1")
        store.restore_previous_resume("uid-1")
        assert store.get_resume("uid-1") == b"v2"
        assert store.get_previous_resume("uid-1") == b"v1"

    def test_restore_with_no_backup_returns_false(self, store):
        store.save_resume("uid-1", b"v1")
        assert store.restore_previous_resume("uid-1") is False
        assert store.get_resume("uid-1") == b"v1"

    def test_restore_with_nothing_at_all_returns_false(self, store):
        assert store.restore_previous_resume("uid-nobody") is False


class TestProfileOps:
    def test_save_and_get(self, store):
        store.save_profile("uid-1", "# My Profile\n\nHello world")
        assert "Hello world" in store.get_profile("uid-1")

    def test_missing_returns_none(self, store):
        assert store.get_profile("uid-nobody") is None
