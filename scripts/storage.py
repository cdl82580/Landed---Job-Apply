"""
scripts/storage.py — Tigris (S3-compatible) storage for user data and resumes.

Key layout:
  users/{sha256(email)}.json          — account record
  user_ids/{user_id}.txt              — reverse lookup: user_id → email
  resumes/{user_id}/master.docx       — uploaded master resume
  profiles/{user_id}/profile.md       — voice / profile guide
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

try:
    import boto3
    import botocore.config
    from botocore.exceptions import ClientError
    _HAS_BOTO3 = True
except ImportError:
    _HAS_BOTO3 = False

_BOTO_CONFIG = botocore.config.Config(
    connect_timeout=5,
    read_timeout=10,
    max_pool_connections=20,
) if _HAS_BOTO3 else None

BUCKET    = os.environ.get("BUCKET_NAME", "job-apply-storage")
_ENDPOINT = os.environ.get("AWS_ENDPOINT_URL_S3", "https://fly.storage.tigris.dev")
_REGION   = os.environ.get("AWS_REGION", "auto")


def is_configured() -> bool:
    return _HAS_BOTO3 and bool(os.environ.get("AWS_ACCESS_KEY_ID"))


_s3_client = None


def _client():
    global _s3_client
    if not _HAS_BOTO3:
        raise RuntimeError("boto3 not installed — run: pip install boto3")
    if _s3_client is None:
        _s3_client = boto3.client("s3", endpoint_url=_ENDPOINT, region_name=_REGION,
                                  config=_BOTO_CONFIG)
    return _s3_client


def _email_key(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


# ---------------------------------------------------------------------------
# Low-level primitives
# ---------------------------------------------------------------------------

def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    _client().put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=content_type)


def delete_bytes(key: str) -> None:
    _client().delete_object(Bucket=BUCKET, Key=key)


def put_text(key: str, text: str) -> None:
    put_bytes(key, text.encode("utf-8"), "text/plain; charset=utf-8")


def delete_text(key: str) -> None:
    delete_bytes(key)


def list_keys(prefix: str) -> list[str]:
    """Return all object keys under prefix (paginates automatically)."""
    try:
        paginator = _client().get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys
    except Exception:
        logger.exception("list_keys failed for prefix %r", prefix)
        return []


def get_bytes(key: str) -> bytes | None:
    try:
        resp = _client().get_object(Bucket=BUCKET, Key=key)
        return resp["Body"].read()
    except Exception as e:
        if hasattr(e, "response") and e.response.get("Error", {}).get("Code") == "NoSuchKey":
            return None
        raise


def get_text(key: str) -> str | None:
    data = get_bytes(key)
    return data.decode("utf-8") if data is not None else None


def exists(key: str) -> bool:
    try:
        _client().head_object(Bucket=BUCKET, Key=key)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# User accounts
# ---------------------------------------------------------------------------

def get_user_by_email(email: str) -> dict[str, Any] | None:
    data = get_text(f"users/{_email_key(email)}.json")
    return json.loads(data) if data else None


def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    email = get_text(f"user_ids/{user_id}.txt")
    return get_user_by_email(email.strip()) if email else None


def list_all_users() -> list[dict[str, Any]]:
    """Scan user_ids/ prefix and return all user records. Admin use only."""
    from . import cache
    cached = cache.get("all_users")
    if cached is not None:
        return cached
    try:
        paginator = _client().get_paginator("list_objects_v2")
        users = []
        for page in paginator.paginate(Bucket=BUCKET, Prefix="user_ids/"):
            for obj in page.get("Contents", []):
                # key = "user_ids/{user_id}.txt"
                user_id = obj["Key"].split("/", 1)[1].removesuffix(".txt")
                if not user_id:
                    continue
                u = get_user_by_id(user_id)
                if u:
                    users.append(u)
        return cache.put("all_users", users)
    except Exception:
        logger.exception("list_all_users failed — check S3 credentials and bucket config")
        return []


def get_user_by_google_id(google_id: str) -> dict[str, Any] | None:
    """Look up a user by their Google OAuth sub (stored in user record as google_id).
    Uses an index file to avoid scanning all user records."""
    email = get_text(f"google_ids/{google_id}.txt")
    if email:
        return get_user_by_email(email.strip())
    return None


def update_user_email(user: dict[str, Any], new_email: str) -> None:
    """Rename a user's email, updating all index keys atomically (best-effort)."""
    old_email = user["email"].strip().lower()
    new_email  = new_email.strip().lower()
    if old_email == new_email:
        return
    # Remove old key
    try:
        delete_bytes(f"users/{_email_key(old_email)}.json")
    except Exception:
        pass
    user["email"] = new_email
    # Write new key + reverse index
    put_text(f"users/{_email_key(new_email)}.json", json.dumps(user))
    put_text(f"user_ids/{user['user_id']}.txt", new_email)
    if user.get("google_id"):
        put_text(f"google_ids/{user['google_id']}.txt", new_email)


def save_user(user: dict[str, Any]) -> None:
    """Persist a user record. Also writes index entries for user_id and google_id."""
    put_text(f"users/{_email_key(user['email'])}.json", json.dumps(user))
    put_text(f"user_ids/{user['user_id']}.txt", user["email"])
    if user.get("google_id"):
        put_text(f"google_ids/{user['google_id']}.txt", user["email"])
    from . import cache
    cache.invalidate("all_users")


# ---------------------------------------------------------------------------
# Resumes and profiles
# ---------------------------------------------------------------------------

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def save_resume(user_id: str, data: bytes) -> bool:
    """Save the user's master resume, first stashing whatever was there as a
    single-slot backup (resumes/{user_id}/master_previous.docx) so an
    overwrite can be undone. Returns True if a previous resume existed and
    was backed up, False if this was the user's first resume."""
    existing = get_bytes(f"resumes/{user_id}/master.docx")
    backed_up = existing is not None
    if backed_up:
        put_bytes(f"resumes/{user_id}/master_previous.docx", existing, _DOCX_MIME)
    put_bytes(f"resumes/{user_id}/master.docx", data, _DOCX_MIME)
    return backed_up


def get_resume(user_id: str) -> bytes | None:
    return get_bytes(f"resumes/{user_id}/master.docx")


def has_resume(user_id: str) -> bool:
    from . import cache
    ck = f"has_resume:{user_id}"
    cached = cache.get(ck)
    if cached is not None:
        return cached
    return cache.put(ck, exists(f"resumes/{user_id}/master.docx"))


def get_previous_resume(user_id: str) -> bytes | None:
    return get_bytes(f"resumes/{user_id}/master_previous.docx")


def has_previous_resume(user_id: str) -> bool:
    return exists(f"resumes/{user_id}/master_previous.docx")


def restore_previous_resume(user_id: str) -> bool:
    """Swap the backup back to current. Goes through save_resume so the
    just-replaced current version becomes the new backup — a symmetric
    swap, not a delete, so restoring twice returns to the original state.
    Returns False if there's no backup to restore."""
    previous = get_bytes(f"resumes/{user_id}/master_previous.docx")
    if previous is None:
        return False
    save_resume(user_id, previous)
    return True


def save_profile(user_id: str, text: str) -> None:
    put_text(f"profiles/{user_id}/profile.md", text)


def get_profile(user_id: str) -> str | None:
    return get_text(f"profiles/{user_id}/profile.md")
