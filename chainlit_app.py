"""
chainlit_app.py — KB support chatbot, mounted into api.py at /chat.

Auth: header_auth_callback re-validates the same signed "session" cookie
api.py issues (scripts/session.py), so only users already logged into the
main app can open the widget. The outer FastAPI auth_middleware in api.py
already blocks anonymous requests to /chat/* before they ever reach here —
this is a second, independent check that also gives us the user's role.

Retrieval: no vector store. The whole visible KB (filtered by adminOnly vs
the caller's role, same rule kb.html applies client-side) is loaded fresh at
chat start and stuffed into the system prompt. Revisit with embeddings if
the KB grows large enough that this stops fitting comfortably in context.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Dict, Optional

import anthropic
import chainlit as cl

from routers.kb import _load as _load_kb
from scripts import chat_logs as log_store
from scripts.session import resolve_session_secret, verify_session_token

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"
_SESSION_COOKIE_NAME = "session"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

_SYSTEM_PROMPT_HEADER = """You are the support assistant for Landed, embedded as a chat widget \
in the web app. Answer only using the reference material provided below — \
if the answer isn't covered by it, say you're not sure and point the user to \
the Knowledge Base at /kb.html rather than guessing. Keep answers short and direct.

Call the flag_for_team tool (silently, in the background) once you have \
enough detail about one of the following:
- You can't answer the user's question from the reference material below, or
- The user is reporting a bug or something broken, or
- The user wants to leave feedback or a suggestion about Landed

Before calling it for a bug report or an unanswerable question, ask ONE \
short follow-up first to get useful detail — e.g. what page or action \
they were on, whether it happens every time, any error text they saw, and \
whether they can attach a screenshot (there's a paperclip in the message \
box). Don't interrogate them — one round of clarifying questions is \
enough; call the tool with whatever you have after that even if some \
detail is still missing. Skip the follow-up entirely if the user already \
gave enough detail up front, or for straightforward feedback that doesn't \
need clarifying.

This quietly notifies the team so a human can follow up — it does not \
send anything to the user and the user should never be told an email or \
notification is being sent. Any screenshots or files the user attaches \
are automatically forwarded to the team when you call the tool — you \
don't need to describe or relay their contents yourself, just look at \
images if it helps you understand the issue. After calling the tool, \
reply naturally (e.g. acknowledge you've noted it / passed it along, or \
that someone will follow up) — never mention email, notifications, or \
"the team's inbox" by name.
"""

_README_PATH = Path(__file__).resolve().parent / "README.md"

# ---------------------------------------------------------------------------
# Anthropic client — lazy init, same pattern as apply.py
# ---------------------------------------------------------------------------

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


# ---------------------------------------------------------------------------
# Local sliding-window rate limiter (per user_id) — a real Anthropic call
# happens per message, so this is a cheap cost/abuse guard. Copied rather
# than imported from api.py to avoid import-order fragility: api.py is what
# triggers the dynamic load of this module via mount_chainlit().
# ---------------------------------------------------------------------------

_RATE_LIMIT_MAX = 20
_RATE_LIMIT_WINDOW_SECS = 300
_rl_buckets: Dict[str, list[float]] = {}


def _rate_limited(key: str) -> bool:
    """Return True if `key` has exceeded the window's message budget."""
    now = time.time()
    hits = [t for t in _rl_buckets.get(key, []) if now - t < _RATE_LIMIT_WINDOW_SECS]
    if len(hits) >= _RATE_LIMIT_MAX:
        _rl_buckets[key] = hits
        return True
    hits.append(now)
    _rl_buckets[key] = hits
    return False


# ---------------------------------------------------------------------------
# Escalation — a tool Claude can call to silently notify the team when it
# can't answer from the KB, or the user reports a bug / leaves feedback.
# Separate, tighter rate limit from the general chat one above so a chatty
# session can't turn into a flood of emails.
# ---------------------------------------------------------------------------

_FLAG_RATE_LIMIT_MAX = 3
_FLAG_RATE_LIMIT_WINDOW_SECS = 3600
_flag_buckets: Dict[str, list[float]] = {}


def _flag_rate_limited(key: str) -> bool:
    now = time.time()
    hits = [t for t in _flag_buckets.get(key, []) if now - t < _FLAG_RATE_LIMIT_WINDOW_SECS]
    if len(hits) >= _FLAG_RATE_LIMIT_MAX:
        _flag_buckets[key] = hits
        return True
    hits.append(now)
    _flag_buckets[key] = hits
    return False


_NOTIFY_TO_ADDRESS = os.environ.get("SUPPORT_NOTIFY_EMAIL", "cdl825@gmail.com")
_NOTIFY_FROM_ADDRESS = os.environ.get("RESEND_FROM", "Landed <hello@cdlav.us>")

# Uploaded files are forwarded to the team as email attachments — capped so
# one chat session can't build an oversized/expensive email.
_MAX_ATTACHMENTS = 5
_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024

FLAG_FOR_TEAM_TOOL = {
    "name": "flag_for_team",
    "description": (
        "Silently notify the Landed team in the background. Use this when you "
        "can't answer the user's question from the reference material, when "
        "the user reports a bug, or when they offer feedback/a suggestion. "
        "This never surfaces anything to the user — don't mention it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "enum": ["unanswered_question", "bug_report", "feedback"],
                "description": "Why this is being flagged.",
            },
            "summary": {
                "type": "string",
                "description": "One-line summary of the question, bug, or feedback.",
            },
        },
        "required": ["reason", "summary"],
    },
}

_FLAG_REASON_LABELS = {
    "unanswered_question": "Unanswered question",
    "bug_report": "Bug report",
    "feedback": "Feedback",
}


def _read_element_bytes(el) -> Optional[bytes]:
    if el.content:
        return el.content if isinstance(el.content, bytes) else el.content.encode("utf-8")
    if el.path:
        try:
            return Path(el.path).read_bytes()
        except OSError:
            return None
    return None


def _collect_uploads(elements) -> list[dict]:
    """Read any files/screenshots attached to a message into memory now, since
    Chainlit's on-disk copies live in a per-session temp dir that may be gone
    by the time an escalation email fires later in the conversation."""
    uploads = []
    for el in elements or []:
        data = _read_element_bytes(el)
        if not data:
            continue
        uploads.append({
            "name": el.name or "attachment",
            "mime": el.mime or "application/octet-stream",
            "data": data,
        })
    return uploads


def _image_content_block(upload: dict) -> Optional[dict]:
    """Anthropic image content block, so Claude can actually see a screenshot
    the user attached — not just know a file exists."""
    if not upload["mime"].startswith("image/"):
        return None
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": upload["mime"],
            "data": base64.b64encode(upload["data"]).decode("ascii"),
        },
    }


def _render_transcript(log_messages: list) -> str:
    """Human-readable transcript for the escalation email — the flat log,
    not the raw Anthropic history (which gets trimmed for token budget and
    carries tool-call plumbing a human doesn't need)."""
    lines = []
    for m in log_messages:
        line = f"{m['role']}: {m['text']}"
        if m.get("attachments"):
            line += f" [attached: {', '.join(m['attachments'])}]"
        lines.append(line)
    return "\n\n".join(lines)


def _flag_for_team(
    user: Optional[cl.User],
    reason: str,
    summary: str,
    transcript: str,
    attachments: list[dict],
    thread_subject: str,
    thread_root_message_id: str,
) -> tuple[bool, str]:
    """Send a background notification email via Resend. reply_to is set to
    the user's own address, so replying in the team's inbox goes straight to
    them — that's what actually lets an admin continue the conversation by
    email. Message-ID/In-Reply-To/References keep repeated escalations in the
    same chat session threaded as one email, alongside a stable subject line.
    Returns (ok, message_id used — ok is True on a rate-limit skip too, so the
    model doesn't treat it as a failure worth retrying; message_id is "" in
    that case and on failure)."""
    meta = (user.metadata or {}) if user else {}
    user_key = meta.get("user_id", "anonymous")

    if _flag_rate_limited(user_key):
        logger.info("_flag_for_team: rate-limited, skipping send (user=%r)", user_key)
        return True, ""

    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        logger.warning("_flag_for_team: RESEND_API_KEY not set — notification not sent")
        return False, ""

    label = _FLAG_REASON_LABELS.get(reason, reason)
    body = (
        f"Reason: {label}\n"
        f"User: {meta.get('email', 'unknown')} (role: {meta.get('role', 'unknown')})\n\n"
        f"Summary: {summary}\n\n"
        f"Conversation so far:\n{transcript}\n"
    )
    message_id = f"<{uuid.uuid4()}@cdlav.us>"
    headers = {"Message-ID": message_id}
    if thread_root_message_id:
        headers["In-Reply-To"] = thread_root_message_id
        headers["References"] = thread_root_message_id

    payload: dict = {
        "from": _NOTIFY_FROM_ADDRESS,
        "to": [_NOTIFY_TO_ADDRESS],
        "subject": thread_subject,
        "text": body,
        "headers": headers,
    }
    if meta.get("email"):
        payload["reply_to"] = meta["email"]
    usable = [a for a in attachments[-_MAX_ATTACHMENTS:] if len(a["data"]) <= _MAX_ATTACHMENT_BYTES]
    if usable:
        payload["attachments"] = [
            {"filename": a["name"], "content": base64.b64encode(a["data"]).decode("ascii")}
            for a in usable
        ]
    try:
        import requests
        resp = requests.post(
            "https://api.resend.com/emails",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.warning("_flag_for_team: Resend returned %d: %s", resp.status_code, resp.text[:200])
        return ok, message_id
    except Exception:
        logger.exception("_flag_for_team: request failed")
        return False, ""


def _persist_conversation(
    user: Optional[cl.User],
    conversation_id: str,
    started_at: str,
    log_messages: list,
    escalations: list,
) -> None:
    meta = (user.metadata or {}) if user else {}
    user_id = meta.get("user_id")
    if not user_id:
        return
    record = {
        "id": conversation_id,
        "user_id": user_id,
        "user_email": meta.get("email", "unknown"),
        "user_role": meta.get("role", "user"),
        "started_at": started_at,
        "messages": log_messages,
        "escalations": escalations,
    }
    try:
        log_store.save_conversation(record)
    except Exception:
        logger.exception("_persist_conversation: failed to save (conversation=%r)", conversation_id)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _extract_session_cookie(raw_cookie_header: str) -> Optional[str]:
    jar = SimpleCookie()
    jar.load(raw_cookie_header)
    morsel = jar.get(_SESSION_COOKIE_NAME)
    return morsel.value if morsel else None


@cl.header_auth_callback
async def header_auth_callback(headers) -> Optional[cl.User]:
    token = _extract_session_cookie(headers.get("cookie", ""))
    if not token:
        return None
    payload = verify_session_token(token, resolve_session_secret())
    if not payload:
        return None
    return cl.User(
        identifier=payload["email"],
        metadata={
            "user_id": payload["user_id"],
            "email": payload["email"],
            "role": payload.get("role", "user"),
        },
    )


# ---------------------------------------------------------------------------
# KB retrieval — role-filtered, HTML stripped
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")


def _strip_html(html: str) -> str:
    text = _TAG_RE.sub(" ", html or "")
    text = text.replace("&amp;", "&").replace("&nbsp;", " ")
    text = _WS_RE.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def _visible_kb_text(role: str) -> str:
    data = _load_kb()
    categories = data.get("categories", [])
    articles = data.get("articles", [])

    is_admin = role == "admin"
    visible_categories = {
        c["id"] for c in categories if is_admin or not c.get("adminOnly")
    }

    chunks = []
    for a in articles:
        if a.get("category") not in visible_categories:
            continue
        if a.get("adminOnly") and not is_admin:
            continue
        body = _strip_html(a.get("body", ""))
        chunks.append(f"### {a.get('title', 'Untitled')} ({a.get('category', '')})\n{body}")

    return "\n\n".join(chunks) if chunks else "(No knowledge base articles are available.)"


def _admin_project_docs() -> str:
    """README.md — architecture, deployment, and the Slack/Teams command
    reference all live there. Admin-only: it covers internals (webhooks,
    storage layout, secrets) that aren't appropriate for regular users."""
    try:
        return _README_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Chat lifecycle
# ---------------------------------------------------------------------------

_MAX_HISTORY_MESSAGES = 12


@cl.on_chat_start
async def on_chat_start():
    user = cl.user_session.get("user")
    role = (user.metadata or {}).get("role", "user") if user else "user"

    sections = [_SYSTEM_PROMPT_HEADER, "\n# Knowledge Base\n" + _visible_kb_text(role)]
    if role == "admin":
        docs = _admin_project_docs()
        if docs:
            sections.append(
                "\n\n# Project Documentation (README.md — architecture, deployment, "
                "Slack/Teams command reference)\n" + docs
            )
    system_prompt = "".join(sections)
    cl.user_session.set("system_prompt", system_prompt)
    cl.user_session.set("history", [])
    cl.user_session.set("conversation_id", str(uuid.uuid4()))
    cl.user_session.set("started_at", _now_iso())
    cl.user_session.set("log_messages", [])
    cl.user_session.set("escalations", [])
    cl.user_session.set("email_thread", None)
    cl.user_session.set("uploads", [])

    await cl.Message(
        content="Hi! I can help with questions about using Landed."
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    user = cl.user_session.get("user")
    user_id = (user.metadata or {}).get("user_id", "anonymous") if user else "anonymous"

    if _rate_limited(user_id):
        await cl.Message(content="You're sending messages a bit fast — please wait a few minutes and try again.").send()
        return

    system_prompt = cl.user_session.get("system_prompt") or _SYSTEM_PROMPT_HEADER
    history = cl.user_session.get("history") or []
    log_messages = cl.user_session.get("log_messages") or []
    escalations = cl.user_session.get("escalations") or []
    conversation_id = cl.user_session.get("conversation_id") or str(uuid.uuid4())
    started_at = cl.user_session.get("started_at") or _now_iso()

    new_uploads = _collect_uploads(message.elements)
    if new_uploads:
        uploads = (cl.user_session.get("uploads") or []) + new_uploads
        cl.user_session.set("uploads", uploads[-_MAX_ATTACHMENTS:])

    image_blocks = [b for u in new_uploads if (b := _image_content_block(u))]
    if image_blocks:
        history.append({"role": "user", "content": [{"type": "text", "text": message.content}, *image_blocks]})
    else:
        history.append({"role": "user", "content": message.content})

    log_messages.append({
        "role": "user",
        "text": message.content,
        "at": _now_iso(),
        "attachments": [u["name"] for u in new_uploads],
    })

    reply = cl.Message(content="")
    await reply.send()

    # Usually one round (plain answer or answer + a background tool call).
    # Cap it so a confused model can't loop tool calls forever.
    for _ in range(3):
        async with _get_client().messages.stream(
            model=DEFAULT_MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=history,
            tools=[FLAG_FOR_TEAM_TOOL],
        ) as stream:
            async for token in stream.text_stream:
                await reply.stream_token(token)
            final_message = await stream.get_final_message()

        history.append({"role": "assistant", "content": final_message.content})

        tool_calls = [b for b in final_message.content if b.type == "tool_use"]
        if not tool_calls:
            break

        # This round may have streamed visible text before deciding to call the
        # tool (e.g. troubleshooting tips + "let me flag this too") — separate
        # it from the next round's text so they don't run together mid-sentence.
        if any(b.type == "text" and b.text for b in final_message.content):
            await reply.stream_token("\n\n")

        transcript = _render_transcript(log_messages)
        uploads_for_email = cl.user_session.get("uploads") or []
        email_thread = cl.user_session.get("email_thread")

        tool_results = []
        for call in tool_calls:
            reason = call.input.get("reason", "feedback")
            summary = call.input.get("summary", message.content)
            if not email_thread:
                label = _FLAG_REASON_LABELS.get(reason, reason)
                email_thread = {"subject": f"[Landed KB Bot] {label}: {summary[:80]}", "root_message_id": ""}

            ok, msg_id = _flag_for_team(
                user, reason, summary, transcript, uploads_for_email,
                email_thread["subject"], email_thread["root_message_id"],
            )
            if ok and not email_thread["root_message_id"] and msg_id:
                email_thread["root_message_id"] = msg_id
            cl.user_session.set("email_thread", email_thread)

            escalations.append({
                "reason": reason,
                "summary": summary,
                "at": _now_iso(),
                "email_subject": email_thread["subject"],
                "sent_ok": ok,
            })

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": "Logged." if ok else "Not logged — continue naturally without mentioning this.",
            })
        history.append({"role": "user", "content": tool_results})

    log_messages.append({"role": "assistant", "text": reply.content, "at": _now_iso()})

    cl.user_session.set("history", history[-_MAX_HISTORY_MESSAGES:])
    cl.user_session.set("log_messages", log_messages)
    cl.user_session.set("escalations", escalations)
    _persist_conversation(user, conversation_id, started_at, log_messages, escalations)

    await reply.update()
