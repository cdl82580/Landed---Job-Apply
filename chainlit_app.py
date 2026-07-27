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

import re
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Dict, Optional

import anthropic
import chainlit as cl

from routers.kb import _load as _load_kb
from scripts.session import resolve_session_secret, verify_session_token

DEFAULT_MODEL = "claude-sonnet-5"
_SESSION_COOKIE_NAME = "session"

_SYSTEM_PROMPT_HEADER = """You are the support assistant for Landed, embedded as a chat widget \
in the web app. Answer only using the reference material provided below — \
if the answer isn't covered by it, say you're not sure and point the user to \
the Knowledge Base at /kb.html rather than guessing. Keep answers short and direct.
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

    history.append({"role": "user", "content": message.content})

    reply = cl.Message(content="")
    await reply.send()

    full_text = ""
    async with _get_client().messages.stream(
        model=DEFAULT_MODEL,
        max_tokens=1024,
        system=system_prompt,
        messages=history,
    ) as stream:
        async for token in stream.text_stream:
            full_text += token
            await reply.stream_token(token)

    history.append({"role": "assistant", "content": full_text})
    cl.user_session.set("history", history[-_MAX_HISTORY_MESSAGES:])

    await reply.update()
