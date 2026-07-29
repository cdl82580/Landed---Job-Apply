"""
routers/kb.py — Knowledge Base article management.

Public endpoints (any authenticated user):
  GET  /api/kb/articles          — list all articles
  GET  /api/kb/articles/{id}     — get one article

Admin-only endpoints:
  POST   /api/admin/kb/articles              — create article
  PUT    /api/admin/kb/articles/{id}         — update article
  DELETE /api/admin/kb/articles/{id}         — delete (status 204)
  PUT    /api/admin/kb/categories/{id}       — update a category label/desc/icon
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from scripts import storage

router = APIRouter(tags=["kb"])

_KB_KEY = "kb/data.json"


# ---------------------------------------------------------------------------
# Seed data — used when no KB has been saved yet
# ---------------------------------------------------------------------------

_SEED_CATEGORIES = [
    {"id": "getting-started", "label": "Getting Started",      "icon": "🚀", "desc": "First steps, workflow overview, and how the agent produces your documents."},
    {"id": "role-types",      "label": "Role Types & Framing", "icon": "🎯", "desc": "Framing angles and strategy for each role category the agent supports."},
    {"id": "resume",          "label": "Resume Tailoring",     "icon": "📄", "desc": "How the agent customizes the master resume — competencies, bullets, and structure."},
    {"id": "cover-letter",    "label": "Cover Letter",         "icon": "✉️",  "desc": "Structure, tone rules, opening hooks, and evidence paragraph strategy."},
    {"id": "ats",             "label": "ATS Optimization",     "icon": "🤖", "desc": "How the ATS resume is built and what makes it parser-friendly."},
    {"id": "workflow",        "label": "Workflow & Web App",   "icon": "⚙️",  "desc": "Using the web interface, running the agent, and the interview prep workflow."},
    {"id": "slack",           "label": "Slack Integration",    "icon": "💬", "iconImg": "/img/slack-icon.svg", "desc": "Slash commands for running the agent, tracking applications, and calendar management."},
    {"id": "teams",           "label": "Teams Integration",    "icon": "👥", "iconImg": "/img/teams-icon.svg", "desc": "Chat commands for running the agent, tracking applications, and calendar management from Microsoft Teams."},
    {"id": "admin",           "label": "Admin",                "icon": "🔐", "desc": "Webhooks, system architecture, Drive integration, XML editing, and troubleshooting.", "adminOnly": True},
]


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _load() -> dict:
    raw = storage.get_text(_KB_KEY)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {"categories": _SEED_CATEGORIES, "articles": []}


def _save(data: dict) -> None:
    storage.put_text(_KB_KEY, json.dumps(data, ensure_ascii=False))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _require_auth(request: Request) -> dict:
    from api import _require_user  # noqa: PLC0415
    return _require_user(request)


def _require_admin(request: Request) -> dict:
    from api import _require_admin as _ra  # noqa: PLC0415
    return _ra(request)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ArticleCreate(BaseModel):
    id:        str | None = None
    category:  str
    title:     str
    snippet:   str = ""
    read_time: str = "3 min"
    body:      str = ""
    admin_only: bool = False


class ArticleUpdate(BaseModel):
    category:  str | None = None
    title:     str | None = None
    snippet:   str | None = None
    read_time: str | None = None
    body:      str | None = None
    admin_only: bool | None = None
    sort_order: int | None = None


class CategoryCreate(BaseModel):
    id:        str
    label:     str
    icon:      str = ""
    desc:      str = ""
    admin_only: bool = False


class CategoryUpdate(BaseModel):
    label:     str | None = None
    icon:      str | None = None
    desc:      str | None = None
    admin_only: bool | None = None


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------

@router.get("/api/kb/articles")
async def list_articles(request: Request):
    _require_auth(request)
    data = _load()
    return {
        "categories": data.get("categories", _SEED_CATEGORIES),
        "articles":   data.get("articles", []),
    }


@router.get("/api/kb/articles/{article_id}")
async def get_article(article_id: str, request: Request):
    _require_auth(request)
    data = _load()
    for a in data.get("articles", []):
        if a["id"] == article_id:
            return a
    raise HTTPException(404, "Article not found")


@router.get("/api/kb/categories")
async def list_categories(request: Request):
    _require_auth(request)
    data = _load()
    return data.get("categories", _SEED_CATEGORIES)


# ---------------------------------------------------------------------------
# Admin endpoints — articles
# ---------------------------------------------------------------------------

@router.post("/api/admin/kb/articles", status_code=201)
async def create_article(body: ArticleCreate, request: Request):
    admin = _require_admin(request)
    data = _load()
    articles = data.get("articles", [])

    art_id = (body.id or "").strip() or str(uuid.uuid4())[:8]
    if any(a["id"] == art_id for a in articles):
        raise HTTPException(400, f"Article ID '{art_id}' already exists")

    now = _now()
    article: dict[str, Any] = {
        "id":         art_id,
        "category":   body.category,
        "title":      body.title.strip(),
        "snippet":    body.snippet.strip(),
        "readTime":   body.read_time,
        "body":       body.body,
        "adminOnly":  body.admin_only,
        "created_at": now,
        "updated_at": now,
        "created_by": admin["email"],
    }
    articles.append(article)
    data["articles"] = articles
    _save(data)
    return article


@router.put("/api/admin/kb/articles/{article_id}")
async def update_article(article_id: str, body: ArticleUpdate, request: Request):
    admin = _require_admin(request)
    data = _load()
    articles = data.get("articles", [])

    for i, a in enumerate(articles):
        if a["id"] == article_id:
            if body.category  is not None: a["category"]  = body.category
            if body.title     is not None: a["title"]      = body.title.strip()
            if body.snippet   is not None: a["snippet"]    = body.snippet.strip()
            if body.read_time is not None: a["readTime"]   = body.read_time
            if body.body      is not None: a["body"]       = body.body
            if body.admin_only is not None: a["adminOnly"] = body.admin_only
            a["updated_at"] = _now()
            a["updated_by"] = admin["email"]
            articles[i] = a
            data["articles"] = articles
            _save(data)
            return a

    raise HTTPException(404, "Article not found")


@router.delete("/api/admin/kb/articles/{article_id}", status_code=204)
async def delete_article(article_id: str, request: Request):
    _require_admin(request)
    data = _load()
    before = len(data.get("articles", []))
    data["articles"] = [a for a in data.get("articles", []) if a["id"] != article_id]
    if len(data["articles"]) == before:
        raise HTTPException(404, "Article not found")
    _save(data)


# ---------------------------------------------------------------------------
# Admin endpoints — seed from frontend KB const
# ---------------------------------------------------------------------------

class SeedPayload(BaseModel):
    categories: list[dict]
    articles:   list[dict]


@router.post("/api/admin/kb/seed", status_code=200)
async def seed_kb(body: SeedPayload, request: Request):
    """Replace the stored KB with the data sent from the frontend seed."""
    admin = _require_admin(request)
    now = _now()
    for a in body.articles:
        a.setdefault("created_at", now)
        a.setdefault("updated_at", now)
        a.setdefault("created_by", admin["email"])
    data = {"categories": body.categories, "articles": body.articles}
    _save(data)
    return {"ok": True, "articles": len(body.articles), "categories": len(body.categories)}


@router.post("/api/admin/kb/seed-from-file", status_code=200)
async def seed_kb_from_file(request: Request):
    """Extract KB data from frontend/kb.html's `const KB = {...}` literal and
    seed it to storage.

    Parses the object literal directly in Python (scripts/js_object_parser)
    instead of eval()-ing it in a spawned Node process. The old approach ran
    whatever JavaScript happened to be in the file through eval() — harmless
    only as long as kb.html can never be written by anything less trusted
    than this endpoint's own admin-only callers. The parser below only ever
    produces plain data (strings/numbers/lists/dicts) or raises; it has no
    way to execute code, so this can no longer become a code-execution path
    even if that assumption ever stops holding.
    """
    import os  # noqa: PLC0415

    from scripts.js_object_parser import (  # noqa: PLC0415
        JSObjectParseError, extract_balanced_braces, parse_js_object,
    )

    admin = _require_admin(request)

    kb_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "kb.html")
    kb_path = os.path.abspath(kb_path)

    if not os.path.exists(kb_path):
        raise HTTPException(500, "frontend/kb.html not found on server")

    with open(kb_path, encoding="utf-8") as f:
        html = f.read()
    marker = "const KB = "
    marker_pos = html.find(marker + "{")
    if marker_pos == -1:
        raise HTTPException(500, "KB const not found in frontend/kb.html")

    try:
        obj_src = extract_balanced_braces(html, marker_pos + len(marker))
        kb_data = parse_js_object(obj_src)
    except JSObjectParseError as exc:
        raise HTTPException(500, f"KB parsing failed: {exc}")

    now = _now()
    articles = []
    for a in kb_data.get("articles", []):
        articles.append({
            "id":         a.get("id", ""),
            "category":   a.get("category", ""),
            "title":      a.get("title", ""),
            "snippet":    a.get("snippet", ""),
            "readTime":   a.get("readTime", "3 min"),
            "body":       a.get("body", ""),
            "adminOnly":  bool(a.get("adminOnly", False)),
            "created_at": now,
            "updated_at": now,
            "created_by": admin["email"],
        })

    categories = [
        {
            "id":        c.get("id", ""),
            "label":     c.get("label", ""),
            "icon":      c.get("icon", ""),
            "iconImg":   c.get("iconImg", ""),
            "desc":      c.get("desc", ""),
            "adminOnly": bool(c.get("adminOnly", False)),
        }
        for c in kb_data.get("categories", _SEED_CATEGORIES)
    ]

    data = {"categories": categories, "articles": articles}
    _save(data)
    return {"ok": True, "articles": len(articles), "categories": len(categories)}


# ---------------------------------------------------------------------------
# Admin endpoints — categories
# ---------------------------------------------------------------------------

@router.post("/api/admin/kb/categories", status_code=201)
async def create_category(body: CategoryCreate, request: Request):
    _require_admin(request)
    data = _load()
    categories = data.get("categories", list(_SEED_CATEGORIES))
    cat_id = body.id.strip().lower().replace(" ", "-")
    if not cat_id:
        raise HTTPException(400, "Category ID is required")
    if any(c["id"] == cat_id for c in categories):
        raise HTTPException(400, f"Category ID '{cat_id}' already exists")
    cat = {
        "id":        cat_id,
        "label":     body.label.strip(),
        "icon":      body.icon,
        "desc":      body.desc,
        "adminOnly": body.admin_only,
    }
    categories.append(cat)
    data["categories"] = categories
    _save(data)
    return cat


@router.delete("/api/admin/kb/categories/{category_id}", status_code=204)
async def delete_category(category_id: str, request: Request):
    _require_admin(request)
    data = _load()
    categories = data.get("categories", list(_SEED_CATEGORIES))
    if not any(c["id"] == category_id for c in categories):
        raise HTTPException(404, "Category not found")
    data["categories"] = [c for c in categories if c["id"] != category_id]
    _save(data)


@router.put("/api/admin/kb/categories/{category_id}")
async def update_category(category_id: str, body: CategoryUpdate, request: Request):
    admin = _require_admin(request)
    data = _load()
    categories = data.get("categories", list(_SEED_CATEGORIES))

    for i, c in enumerate(categories):
        if c["id"] == category_id:
            if body.label     is not None: c["label"]     = body.label
            if body.icon      is not None: c["icon"]      = body.icon
            if body.desc      is not None: c["desc"]      = body.desc
            if body.admin_only is not None: c["adminOnly"] = body.admin_only
            categories[i] = c
            data["categories"] = categories
            _save(data)
            return c

    raise HTTPException(404, "Category not found")
