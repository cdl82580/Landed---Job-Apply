"""
scripts/webhooks.py — Webhook storage and delivery.

Storage layout:
  webhooks/_index.json          — summary list
  webhooks/{id}.json            — full webhook record including recent deliveries

Delivery:
  - dispatch_async() is called after every audit event
  - Matching webhooks (by subscribed events) are fired in a background thread
  - HMAC-SHA256 signature sent as X-Hub-Signature-256 header (GitHub format)
  - Last 25 deliveries kept per webhook for the admin dashboard
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.parse
import uuid
from typing import Any

import requests as _requests

from . import storage
from .ssrf import is_ssrf_url as _is_ssrf_url

_INDEX_KEY    = "webhooks/_index.json"

# ---------------------------------------------------------------------------
# Secret encryption — AES-256-GCM via stdlib only (no cryptography dep)
# Key derived from SESSION_SECRET so secrets are useless without it.
# ---------------------------------------------------------------------------
import base64 as _base64
import os as _os
import struct as _struct


def _secret_key() -> bytes:
    """32-byte key derived from SESSION_SECRET via SHA-256."""
    raw = _os.environ.get("SESSION_SECRET", "")
    if not raw:
        raise RuntimeError("SESSION_SECRET is not set — required to encrypt webhook secrets")
    return hashlib.sha256(raw.encode()).digest()


def _encrypt_secret(plaintext: str) -> str:
    """Encrypt a webhook secret string. Returns 'enc:v1:<b64(nonce+tag+ct)>'."""
    if not plaintext:
        return plaintext
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = _os.urandom(12)
        ct_and_tag = AESGCM(_secret_key()).encrypt(nonce, plaintext.encode(), None)
        blob = _base64.b64encode(nonce + ct_and_tag).decode()
        return f"enc:v1:{blob}"
    except ImportError:
        # cryptography package not installed — store plaintext (no encryption available)
        return plaintext


def _decrypt_secret(stored: str) -> str:
    """Decrypt a webhook secret previously encrypted by _encrypt_secret."""
    if not stored or not stored.startswith("enc:v1:"):
        return stored  # legacy plaintext or empty
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = _base64.b64decode(stored[7:])
        nonce, ct_and_tag = raw[:12], raw[12:]
        return AESGCM(_secret_key()).decrypt(nonce, ct_and_tag, None).decode()
    except Exception:
        return ""  # decryption failed — treat as no secret

# ---------------------------------------------------------------------------
# Action categories — used for the category filter
# ---------------------------------------------------------------------------

CATEGORY_ACTIONS: dict[str, set[str]] = {
    "auth": {
        "user_registered", "user_registered_google", "google_account_linked",
        "login_success", "login_google", "login_failed", "logout",
        "email_verified", "verification_email_resent", "password_changed",
    },
    "profile": {
        "profile_updated", "resume_uploaded", "email_changed",
    },
    "applications": {
        "created", "updated", "deleted",
        "comment_added", "comment_edited", "comment_deleted",
        "run_linked", "run_unlinked", "imported",
        "admin_updated", "admin_deleted",
        "admin_comment_added", "admin_comment_edited", "admin_comment_deleted",
        "match_scored", "jd_extracted", "setup_folder_started",
        "jd_capture_started", "jd_capture_completed", "jd_capture_failed",
    },
    "calendar": {
        "calendar_event_created", "calendar_event_updated", "calendar_event_deleted",
    },
    "runs": {
        "run_started", "run_completed", "run_failed",
        "prep_started", "prep_completed", "prep_failed",
        "file_downloaded",
    },
    "admin": {
        "role_changed", "admin_user_updated", "admin_verification_resent",
        "admin_csv_export",
        "webhook_created", "webhook_updated", "webhook_deleted", "webhook_tested",
    },
    "notifications": {
        "notification_sent", "notification_action_taken",
    },
}
_MAX_DELIVERIES = 25
_INDEX_FIELDS = {"id", "name", "url", "events", "active", "created_at",
                 "last_triggered_at", "delivery_stats"}

_index_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _key(webhook_id: str) -> str:
    return f"webhooks/{webhook_id}.json"


def _read_index() -> list[dict[str, Any]]:
    raw = storage.get_text(_INDEX_KEY)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _write_index(entries: list[dict[str, Any]]) -> None:
    storage.put_text(_INDEX_KEY, json.dumps(entries))


def _to_index_entry(w: dict[str, Any]) -> dict[str, Any]:
    return {k: w[k] for k in _INDEX_FIELDS if k in w}


def _upsert_index(webhook: dict[str, Any]) -> None:
    with _index_lock:
        index = _read_index()
        entry = _to_index_entry(webhook)
        for i, e in enumerate(index):
            if e["id"] == webhook["id"]:
                index[i] = entry
                break
        else:
            index.append(entry)
        _write_index(index)


def _remove_index(webhook_id: str) -> None:
    with _index_lock:
        index = [e for e in _read_index() if e["id"] != webhook_id]
        _write_index(index)


# ---------------------------------------------------------------------------
# Public CRUD
# ---------------------------------------------------------------------------

def list_webhooks() -> list[dict[str, Any]]:
    return _read_index()


def get_webhook(webhook_id: str) -> dict[str, Any] | None:
    raw = storage.get_text(_key(webhook_id))
    if not raw:
        return None
    try:
        w = json.loads(raw)
        # Decrypt secret for in-memory use; callers must redact before returning to API
        if w.get("secret"):
            w["secret"] = _decrypt_secret(w["secret"])
        return w
    except Exception:
        return None


def save_webhook(webhook: dict[str, Any]) -> None:
    to_store = dict(webhook)
    if to_store.get("secret"):
        to_store["secret"] = _encrypt_secret(to_store["secret"])
    storage.put_text(_key(webhook["id"]), json.dumps(to_store))
    _upsert_index(webhook)  # index never stores the secret


def delete_webhook(webhook_id: str) -> bool:
    if not storage.exists(_key(webhook_id)):
        return False
    storage.delete_bytes(_key(webhook_id))
    _remove_index(webhook_id)
    return True


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def _passes_filters(webhook: dict[str, Any], event: dict[str, Any]) -> bool:
    """Return True if the event passes all configured webhook filters."""
    action = event.get("action", "")

    # ── Event type filter ────────────────────────────────────────────
    events = webhook.get("events") or ["*"]
    if "*" not in events and action not in events:
        return False

    # ── Actor filter (email or user_id, comma-separated) ────────────
    filter_actors = [a.strip() for a in (webhook.get("filter_actors") or "").split(",") if a.strip()]
    if filter_actors:
        actor   = event.get("actor", "")
        user_id = event.get("user_id", "")
        if not any(f in (actor, user_id) for f in filter_actors):
            return False

    # ── Source filter (user | application) ──────────────────────────
    filter_source = (webhook.get("filter_source") or "").strip()
    if filter_source:
        if event.get("source", "") != filter_source:
            return False

    # ── Category filter ──────────────────────────────────────────────
    filter_cats = [c.strip() for c in (webhook.get("filter_categories") or []) if c.strip()]
    if filter_cats:
        in_cat = any(action in CATEGORY_ACTIONS.get(cat, set()) for cat in filter_cats)
        if not in_cat:
            return False

    # ── Application ID filter ────────────────────────────────────────
    filter_app_id = (webhook.get("filter_app_id") or "").strip()
    if filter_app_id:
        if event.get("entity_id", "") != filter_app_id:
            return False

    return True


def dispatch_async(event: dict[str, Any]) -> None:
    """Fire matching webhooks for this event in a daemon thread. Never raises."""
    if not storage.is_configured():
        return
    try:
        active  = [w for w in _read_index() if w.get("active")]
        targets = [w for w in active if _passes_filters(w, event)]
        if targets:
            threading.Thread(
                target=_deliver_batch,
                args=(targets, event),
                daemon=True,
            ).start()
    except Exception:
        pass


def _deliver_batch(targets: list[dict], event: dict) -> None:
    for t in targets:
        try:
            full = get_webhook(t["id"])
            if full and full.get("active"):
                _deliver(full, event)
        except Exception:
            pass


def _deliver(webhook: dict[str, Any], event: dict[str, Any]) -> None:
    """POST payload to webhook URL; record delivery outcome."""
    wid         = webhook["id"]
    delivery_id = str(uuid.uuid4())
    now_ts      = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    start       = time.time()

    fmt = webhook.get("payload_format", "generic")

    if fmt == "slack":
        action   = event.get("action", "unknown")
        actor    = event.get("actor") or event.get("user_email") or "—"
        source   = event.get("source") or "—"
        ip       = event.get("ip") or "—"
        entity   = event.get("entity_label") or ""
        details  = event.get("details") or {}

        # Emoji per action category
        _CAT_EMOJI = {
            "auth":         "🔐",
            "profile":      "👤",
            "applications": "📋",
            "runs":         "🤖",
            "admin":        "🛠️",
        }
        cat_emoji = "📡"
        for cat, actions in CATEGORY_ACTIONS.items():
            if action in actions:
                cat_emoji = _CAT_EMOJI.get(cat, "📡")
                break

        # Human-readable timestamp
        try:
            import datetime as _dt
            ts_human = _dt.datetime.strptime(now_ts, "%Y-%m-%dT%H:%M:%SZ").strftime("%-m/%-d/%y, %-I:%M %p UTC")
        except Exception:
            ts_human = now_ts

        # Fallback text for push notifications
        fallback = f"[Job Apply] {cat_emoji} {action} — {actor}"

        # Header section
        blocks: list[dict] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{cat_emoji}  *`{action}`*"},
            },
            {"type": "divider"},
        ]

        # Core fields
        fields = [
            {"type": "mrkdwn", "text": f"*Actor*\n{actor}"},
            {"type": "mrkdwn", "text": f"*Time*\n{ts_human}"},
            {"type": "mrkdwn", "text": f"*Source*\n{source}"},
            {"type": "mrkdwn", "text": f"*IP*\n{ip}"},
        ]
        if entity:
            fields.append({"type": "mrkdwn", "text": f"*Entity*\n{entity}"})
        blocks.append({"type": "section", "fields": fields[:10]})  # Slack max 10

        # Details context row
        if details:
            det_parts = [f"`{k}`: {v}" for k, v in details.items() if v is not None]
            if det_parts:
                det_text = "  ·  ".join(det_parts)
                if len(det_text) > 300:
                    det_text = det_text[:297] + "…"
                blocks.append({
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"*Details:*  {det_text}"}],
                })

        payload_dict = {"text": fallback, "blocks": blocks}

    elif fmt == "ms_teams":
        # MS Teams Incoming Webhook — MessageCard format
        action  = event.get("action", "unknown")
        actor   = event.get("actor") or event.get("user_email") or "—"
        details = event.get("details") or {}
        facts   = [{"name": "Action", "value": action},
                   {"name": "Actor",  "value": actor},
                   {"name": "Time",   "value": now_ts}]
        for k, v in list(details.items())[:5]:
            facts.append({"name": k.replace("_", " ").title(), "value": str(v)})
        payload_dict = {
            "@type":    "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": "1A3C5E",
            "summary":  f"Job Apply — {action}",
            "sections": [{
                "activityTitle":    f"**Job Apply** · `{action}`",
                "activitySubtitle": f"Actor: {actor}",
                "facts": facts,
                "markdown": True,
            }],
        }

    elif fmt == "grafana_loki":
        # Grafana Loki push API format
        ts_ns = str(int(time.time() * 1_000_000_000))
        log_line = json.dumps({"action": event.get("action"), "actor": event.get("actor"),
                               "details": event.get("details"), "app": "job-apply"})
        payload_dict = {
            "streams": [{
                "stream": {"app": "job-apply", "action": event.get("action", "")},
                "values": [[ts_ns, log_line]],
            }]
        }

    else:
        # Generic — full structured payload
        payload_dict = {
            "webhook_id":  wid,
            "delivery_id": delivery_id,
            "timestamp":   now_ts,
            "app":         "job-apply",
            "event":       event,
        }

    body = json.dumps(payload_dict)

    # Build headers
    headers: dict[str, str] = {
        "Content-Type":   "application/json",
        "User-Agent":     "JobApply-Webhook/1.0",
        "X-Webhook-ID":   wid,
        "X-Delivery-ID":  delivery_id,
    }
    # Admin-supplied custom headers — block Host specifically: the SSRF guard
    # below only validates the URL's own hostname/IP, and a spoofed Host header
    # could route a request to a public multi-tenant IP through to an internal
    # vhost on infrastructure that Host-routes behind that IP (domain fronting).
    custom_headers = {k: v for k, v in (webhook.get("headers") or {}).items()
                       if k.lower() != "host"}
    headers.update(custom_headers)

    secret = (webhook.get("secret") or "").strip()
    if secret:
        sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={sig}"

    # Build URL + query params
    url = webhook["url"]
    qp  = webhook.get("query_params") or {}
    if qp:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(qp)

    # Re-check SSRF at delivery time to guard against DNS rebinding
    if _is_ssrf_url(url):
        error = "Delivery blocked: URL resolved to a private/internal address"
        duration_ms = int((time.time() - start) * 1000)
        delivery: dict[str, Any] = {
            "id":           delivery_id,
            "timestamp":    now_ts,
            "event_action": event.get("action", ""),
            "status_code":  None,
            "success":      False,
            "error":        error,
            "duration_ms":  duration_ms,
        }
        try:
            w = get_webhook(wid)
            if w:
                stats = w.setdefault("delivery_stats", {"total": 0, "success": 0, "failure": 0})
                stats["total"]   = stats.get("total",   0) + 1
                stats["failure"] = stats.get("failure", 0) + 1
                w["last_triggered_at"] = now_ts
                deliveries = w.setdefault("recent_deliveries", [])
                deliveries.insert(0, delivery)
                w["recent_deliveries"] = deliveries[:_MAX_DELIVERIES]
                save_webhook(w)
        except Exception:
            pass
        return

    status_code: int | None = None
    success     = False
    error: str | None = None

    # Retry once on network errors or 5xx responses (transient failures).
    for attempt in range(2):
        try:
            resp = _requests.post(url, data=body, headers=headers, timeout=10, allow_redirects=False)
            status_code = resp.status_code
            success     = 200 <= status_code < 300
            error       = None
            if success or status_code < 500:
                break
            error = f"HTTP {status_code}"
        except Exception as exc:
            error = str(exc)
        if attempt == 0:
            time.sleep(2)

    duration_ms = int((time.time() - start) * 1000)

    delivery: dict[str, Any] = {
        "id":           delivery_id,
        "timestamp":    now_ts,
        "event_action": event.get("action", ""),
        "status_code":  status_code,
        "success":      success,
        "error":        error,
        "duration_ms":  duration_ms,
    }

    # Persist delivery record + update stats on the webhook
    try:
        w = get_webhook(wid)
        if w:
            stats = w.setdefault("delivery_stats", {"total": 0, "success": 0, "failure": 0})
            stats["total"]   = stats.get("total",   0) + 1
            stats["success"] = stats.get("success", 0) + (1 if success else 0)
            stats["failure"] = stats.get("failure", 0) + (0 if success else 1)
            w["last_triggered_at"] = now_ts
            deliveries = w.setdefault("recent_deliveries", [])
            deliveries.insert(0, delivery)
            w["recent_deliveries"] = deliveries[:_MAX_DELIVERIES]
            save_webhook(w)
    except Exception:
        pass
