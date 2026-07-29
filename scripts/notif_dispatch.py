"""
scripts/notif_dispatch.py — Event-triggered notification helpers.

Contains send_email / email_html (shared with api.py) plus the two
event-triggered notification senders (new_application, status_changed)
that are called from routers/applications.py at write time.

api.py re-exports send_email / email_html from here so there is only
one implementation. The scanner notifications (researching nudge, digest,
etc.) live in api.py because they need the token factory and a richer set
of imports already present there.
"""

from __future__ import annotations

import html as _html
import logging
import os
import time
from typing import Any

from . import storage, user_audit

logger = logging.getLogger(__name__)

_FROM_ADDRESS     = os.environ.get("RESEND_FROM", "Job Apply <onboarding@resend.dev>")
_APP_URL          = os.environ.get("APP_URL", "https://apply.cdlav.us")
_LOGO_URL         = f"{_APP_URL}/img/logo.png"
_LOGODEV_PUB_KEY  = os.environ.get("LOGODEV_PUBLIC_KEY") or os.environ.get("LOGODEV_API_KEY", "")


def _esc_html(s) -> str:
    """HTML-escape application-supplied text (company, role, location, etc.)
    before interpolating it into an email's HTML body — see api.py's
    _esc_html for the full rationale."""
    return _html.escape(str(s or ""), quote=True)


# ---------------------------------------------------------------------------
# Core email utilities
# ---------------------------------------------------------------------------

def email_html(body_html: str) -> str:
    """Wrap body_html in the branded email shell."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F9FAFB;font-family:system-ui,-apple-system,sans-serif">
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%"
         style="background:#F9FAFB;padding:2rem 1rem">
    <tr><td align="center">
      <table role="presentation" cellpadding="0" cellspacing="0" width="100%"
             style="max-width:520px;background:#FFFFFF;border-radius:10px;
                    border:1px solid #E5E7EB;overflow:hidden">
        <!-- Header -->
        <tr>
          <td style="background:#1A3C5E;padding:1.25rem 1.75rem">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td style="vertical-align:middle;padding-right:.625rem">
                  <img src="{_LOGO_URL}" alt="Job Apply" height="32"
                       style="display:block;border:0">
                </td>
                <td style="vertical-align:middle">
                  <span style="font-family:system-ui,-apple-system,sans-serif;
                               font-size:1.125rem;font-weight:600;color:#FFFFFF;
                               line-height:32px">Job Apply</span>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <!-- Body -->
        <tr>
          <td style="padding:2rem 1.75rem;color:#111827">
            {body_html}
          </td>
        </tr>
        <!-- Footer -->
        <tr>
          <td style="background:#F3F4F6;padding:.875rem 1.75rem;
                     border-top:1px solid #E5E7EB">
            <p style="margin:0;font-size:.75rem;color:#6B7280">
              You're receiving this because you have an account at
              <a href="{_APP_URL}" style="color:#1A3C5E;text-decoration:none">Job Apply</a>.
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def send_email(to: str, subject: str, body: str, html: str | None = None) -> bool:
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        return False
    payload: dict[str, Any] = {
        "from":    _FROM_ADDRESS,
        "to":      [to],
        "subject": subject,
        "text":    body,
    }
    if html:
        payload["html"] = html
    try:
        import requests as _requests
        resp = _requests.post(
            "https://api.resend.com/emails",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        return 200 <= resp.status_code < 300
    except Exception:
        logger.exception("send_email: Resend request failed")
        return False


def _default_notif_prefs() -> dict[str, bool]:
    keys = {
        "researching_nudge", "follow_up_reminder", "gone_silent",
        "status_changed", "new_application", "daily_digest", "weekly_digest",
    }
    return {k: True for k in keys}


def _get_prefs(user_id: str) -> dict[str, bool]:
    user_record = storage.get_user_by_id(user_id) or {}
    return {**_default_notif_prefs(), **user_record.get("notification_prefs", {})}


def _get_user_email(user_id: str) -> str | None:
    notify_email = os.environ.get("APP_USER_EMAIL", "")
    if notify_email:
        return notify_email
    record = storage.get_user_by_id(user_id)
    return record.get("email") if record else None


# ---------------------------------------------------------------------------
# new_application — fires immediately on POST /api/applications
# ---------------------------------------------------------------------------

def notify_new_application(user_id: str, record: dict[str, Any]) -> None:
    """Send a 'new application added' notification if the pref is enabled."""
    try:
        prefs = _get_prefs(user_id)
        if not prefs.get("new_application", True):
            return

        to = _get_user_email(user_id)
        if not to:
            return

        company = record.get("company", "Unknown")
        role    = record.get("role_title", "Unknown")
        status  = record.get("status", "Researching")
        base    = _APP_URL

        subject = f"New application added: {company}"

        logo_html   = _company_logo_html(record)
        status_pill = _status_pill(status)

        # Friendly applied date: "Monday, June 16, 2026"
        applied_html = ""
        if record.get("date_applied"):
            try:
                from datetime import date as _date
                d = _date.fromisoformat(record["date_applied"])
                friendly = d.strftime("%A, %B %-d, %Y")
            except Exception:
                friendly = record["date_applied"]
            applied_html = (
                f'<tr><td style="padding:.375rem 0;color:#6B7280;font-size:.875rem;width:110px">Applied</td>'
                f'<td style="padding:.375rem 0;color:#374151;font-size:.875rem">{_esc_html(friendly)}</td></tr>'
            )

        score_line = ""
        ms = record.get("match_score")
        if ms:
            _score_pill_style: dict[str, tuple[str, str]] = {
                "Strong Match": ("#D1FAE5", "#065F46"),
                "Good Match":   ("#DBEAFE", "#1E40AF"),
                "Stretch":      ("#FEF3C7", "#92400E"),
                "Long Shot":    ("#FEE2E2", "#991B1B"),
            }
            cat = ms.get("category", "")
            s_bg, s_color = _score_pill_style.get(cat, ("#F3F4F6", "#374151"))
            score_line = (
                f'<p style="margin:.75rem 0 0;font-size:.875rem">'
                f'Match score: <strong>{_esc_html(ms["score"])}</strong> &nbsp;'
                f'<span style="display:inline-block;background:{s_bg};color:{s_color};'
                f'padding:.15rem .55rem;border-radius:999px;font-size:.8rem;font-weight:600">'
                f'{_esc_html(cat)}</span></p>'
            )

        company_heading = (
            f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin-bottom:.375rem">'
            f'<tr>'
            f'{"<td style=\"vertical-align:middle;padding-right:.5rem\">" + logo_html + "</td>" if logo_html else ""}'
            f'<td style="vertical-align:middle">'
            f'<h2 style="color:#1A3C5E;margin:0;font-size:1.1rem">New application added</h2>'
            f'</td></tr></table>'
        )

        body_html = f"""
        {company_heading}
        <p style="color:#6B7280;font-size:.875rem;margin:0 0 1.25rem">
          {_esc_html(company)} &mdash; {_esc_html(role)}
        </p>
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;margin-bottom:1.25rem">
          <tr>
            <td style="padding:.375rem 0;color:#6B7280;font-size:.875rem;width:110px">Status</td>
            <td style="padding:.375rem 0">{status_pill}</td>
          </tr>
          {applied_html}
          {'<tr><td style="padding:.375rem 0;color:#6B7280;font-size:.875rem;width:110px">Location</td>'
           f'<td style="padding:.375rem 0;color:#374151;font-size:.875rem">{_esc_html(record["location"])}</td></tr>'
           if record.get("location") else ""}
        </table>
        {score_line}
        <div style="margin-top:1.5rem">
          <a href="{base}/index.html#tracker"
             style="display:inline-block;background:#1A3C5E;color:#fff;text-decoration:none;
                    padding:.625rem 1.25rem;border-radius:6px;font-weight:600;font-size:.9rem">
            Open Tracker &rarr;
          </a>
        </div>
        """

        text = (
            f"New application: {role} at {company} ({status}).\n\n"
            f"View tracker: {base}/index.html#tracker"
        )

        send_email(to, subject, text, html=email_html(body_html))
        logger.info("new_application notification sent user=%s company=%s", user_id, company)
        user_audit.log(user_id, "notification_sent", "system",
                       notification_type="new_application",
                       app_id=record.get("id"), company=company, role_title=role)
    except Exception:
        logger.exception("notify_new_application failed for user=%s", user_id)


# ---------------------------------------------------------------------------
# status_changed — fires immediately on PUT /api/applications when status changes
# ---------------------------------------------------------------------------

_STATUS_EMOJI: dict[str, str] = {
    "Researching":   "&#128270;",
    "Applied":       "&#128228;",
    "Phone Screen":  "&#128222;",
    "Interviewing":  "&#128101;",
    "On Hold":       "&#9203;",
    "Offer":         "&#127881;",
    "No Response":   "&#128683;",
    "Not Applying":  "&#128465;",
    "Rejected":      "&#10060;",
}

_STATUS_BG: dict[str, str] = {
    "Researching":  "#F0F9FF", "Applied":      "#FEF3C7",
    "Phone Screen": "#EDE9FE", "Interviewing": "#DBEAFE",
    "On Hold":      "#FFF7ED", "Offer":        "#D1FAE5",
    "Rejected":     "#FEE2E2", "No Response":  "#F3F4F6",
    "Not Applying": "#F3F4F6",
}
_STATUS_COLOR: dict[str, str] = {
    "Researching":  "#0369A1", "Applied":      "#92400E",
    "Phone Screen": "#5B21B6", "Interviewing": "#1E40AF",
    "On Hold":      "#9A3412", "Offer":        "#065F46",
    "Rejected":     "#991B1B",
}


def _status_pill(status: str) -> str:
    bg    = _STATUS_BG.get(status, "#F3F4F6")
    color = _STATUS_COLOR.get(status, "#374151")
    emoji = _STATUS_EMOJI.get(status, "")
    return (
        f'<span style="display:inline-block;background:{bg};color:{color};'
        f'padding:.2rem .65rem;border-radius:999px;font-size:.85rem;font-weight:600">'
        f'{emoji}&nbsp;{_esc_html(status)}</span>'
    )


def _company_logo_html(record: dict[str, Any], size: int = 32) -> str:
    """Return an <img> tag for the company logo via Logo.dev, or empty string."""
    logo = record.get("company_logo_url", "")
    if not logo:
        domain = record.get("domain", "")
        if domain and _LOGODEV_PUB_KEY:
            logo = f"https://img.logo.dev/{domain}?token={_LOGODEV_PUB_KEY}&size={size}"
    if not logo:
        return ""
    return (
        f'<img src="{_esc_html(logo)}" alt="" width="{size}" height="{size}" '
        f'style="display:inline-block;vertical-align:middle;border-radius:6px;'
        f'border:1px solid #E5E7EB">'
    )


def notify_status_changed(
    user_id: str, record: dict[str, Any], old_status: str, new_status: str
) -> None:
    """Send a 'status changed' notification if the pref is enabled."""
    try:
        prefs = _get_prefs(user_id)
        if not prefs.get("status_changed", True):
            return

        to = _get_user_email(user_id)
        if not to:
            return

        company = record.get("company", "Unknown")
        role    = record.get("role_title", "Unknown")
        base    = _APP_URL

        emoji_new = _STATUS_EMOJI.get(new_status, "&#8594;")
        emoji_old = _STATUS_EMOJI.get(old_status, "&#8594;")
        subject   = f"{company}: {old_status} → {new_status}"

        logo_html       = _company_logo_html(record)
        from_pill       = _status_pill(old_status)
        to_pill         = _status_pill(new_status)

        company_heading = (
            f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin-bottom:.375rem">'
            f'<tr>'
            f'{"<td style=\"vertical-align:middle;padding-right:.5rem\">" + logo_html + "</td>" if logo_html else ""}'
            f'<td style="vertical-align:middle">'
            f'<h2 style="color:#1A3C5E;margin:0;font-size:1.1rem">Status update: {_esc_html(company)}</h2>'
            f'</td></tr></table>'
        )

        body_html = f"""
        {company_heading}
        <p style="color:#6B7280;font-size:.875rem;margin:0 0 1.5rem">
          {_esc_html(role)}
        </p>
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;margin-bottom:1.5rem">
          <tr>
            <td style="padding:.375rem 0;color:#6B7280;font-size:.875rem;width:80px">From</td>
            <td style="padding:.375rem 0">{from_pill}</td>
          </tr>
          <tr>
            <td style="padding:.375rem 0;color:#6B7280;font-size:.875rem">To</td>
            <td style="padding:.375rem 0">{to_pill}</td>
          </tr>
        </table>
        <a href="{base}/index.html#tracker"
           style="display:inline-block;background:#1A3C5E;color:#fff;text-decoration:none;
                  padding:.625rem 1.25rem;border-radius:6px;font-weight:600;font-size:.9rem">
          Open Tracker &rarr;
        </a>
        """

        text = (
            f"{role} at {company}: status changed from {old_status} to {new_status}.\n\n"
            f"View tracker: {base}/index.html#tracker"
        )

        send_email(to, subject, text, html=email_html(body_html))
        logger.info("status_changed notification sent user=%s %s→%s", user_id, old_status, new_status)
        user_audit.log(user_id, "notification_sent", "system",
                       notification_type="status_changed",
                       app_id=record.get("id"), company=company, role_title=role,
                       old_status=old_status, new_status=new_status)
    except Exception:
        logger.exception("notify_status_changed failed for user=%s", user_id)
