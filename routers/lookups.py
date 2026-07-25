"""
routers/lookups.py — server-side proxies for the resume builder's location
and institution autocomplete fields, so third-party API keys stay off the
client and CORS/usage-policy concerns (esp. Nominatim's) are handled in one
place. Same shape as routers/companies.py's Logo.dev proxy: fixed target
hosts (not user-supplied), so no SSRF guard is needed — only the rate limit.

GET /api/lookups/locations?q=boston
Returns up to 6 matches: [{label, city, state, country, country_code}]

GET /api/lookups/institutions?q=harvard
Tries the US Dept of Education College Scorecard API first; if it returns no
results, falls back to the Hipolabs universities API (covers schools outside
the US, which Scorecard doesn't index).
Returns up to 8 matches: [{name, alias, city, state, country, domain}]
"""

from __future__ import annotations

import os

import requests
from fastapi import APIRouter, HTTPException, Query, Request

from scripts import cache

router = APIRouter(prefix="/api/lookups", tags=["lookups"])

_LOOKUP_CACHE_TTL = 300  # seconds

# ---------------------------------------------------------------------------
# Locations — OpenStreetMap Nominatim (free, no key)
# ---------------------------------------------------------------------------

_NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
_CONTACT_EMAIL = os.environ.get("APP_USER_EMAIL", "")
# Nominatim's usage policy requires a descriptive User-Agent identifying the
# application (a generic browser UA is not acceptable) — see
# https://operations.osmfoundation.org/policies/nominatim/
_NOMINATIM_USER_AGENT = "job-apply-corey/1.0" + (f" (contact: {_CONTACT_EMAIL})" if _CONTACT_EMAIL else "")


def _location_label(addr: dict, display_name: str) -> tuple[str, str, str, str, str]:
    city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality") or ""
    state = addr.get("state", "")
    country = addr.get("country", "")
    country_code = addr.get("country_code", "")
    if not city:
        # Fall back to the first segment of display_name rather than dropping
        # the result entirely (covers regions/counties Nominatim didn't tag city on).
        city = display_name.split(",")[0].strip()
    if country_code == "us" and state:
        label = f"{city}, {state}"
    elif state and country:
        label = f"{city}, {state}, {country}"
    elif country:
        label = f"{city}, {country}"
    else:
        label = city
    return label, city, state, country, country_code


@router.get("/locations")
async def search_locations(request: Request, q: str = Query(..., min_length=2)):
    from api import _check_rate_limit  # deferred: api.py imports this router at module load time
    _check_rate_limit(request, "location_search", max_hits=30, window_secs=60)

    cache_key = f"location_search:{q.strip().lower()}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    params = {"q": q, "format": "json", "addressdetails": 1, "limit": 6, "accept-language": "en"}
    if _CONTACT_EMAIL:
        params["email"] = _CONTACT_EMAIL
    try:
        resp = requests.get(
            _NOMINATIM_SEARCH,
            params=params,
            headers={"User-Agent": _NOMINATIM_USER_AGENT},
            timeout=8,
        )
        resp.raise_for_status()
        raw = resp.json()
    except requests.Timeout:
        raise HTTPException(504, "Location search timed out")
    except Exception as exc:
        raise HTTPException(502, f"Location search failed: {exc}")

    results = []
    for item in raw:
        addr = item.get("address", {})
        label, city, state, country, country_code = _location_label(addr, item.get("display_name", ""))
        results.append({
            "label": label, "city": city, "state": state,
            "country": country, "country_code": country_code,
        })

    return cache.put(cache_key, results, ttl=_LOOKUP_CACHE_TTL)


# ---------------------------------------------------------------------------
# Institutions — College Scorecard (US), Hipolabs fallback (international)
# ---------------------------------------------------------------------------

_SCORECARD_SEARCH = "https://api.data.gov/ed/collegescorecard/v1/schools"
_SCORECARD_KEY = os.environ.get("COLLEGE_SCORECARD_API_KEY", "")
_HIPOLABS_SEARCH = "http://universities.hipolabs.com/search"


def _clean_domain(url: str) -> str:
    if not url:
        return ""
    domain = url.strip().lower()
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    domain = domain.split("/", 1)[0]
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _search_scorecard(q: str) -> list[dict] | None:
    """Return normalized results, or None if the API isn't usable/errored
    (so the caller can fall back to Hipolabs) vs. [] for a genuine zero-match."""
    if not _SCORECARD_KEY:
        return None
    try:
        resp = requests.get(
            _SCORECARD_SEARCH,
            params={
                "api_key": _SCORECARD_KEY,
                "school.name": q,
                "fields": "id,school.name,school.city,school.zip,school.state,school.alias,school.school_url",
                "per_page": 8,
            },
            timeout=8,
        )
        resp.raise_for_status()
        raw = resp.json()
    except requests.Timeout:
        raise HTTPException(504, "Institution search timed out")
    except Exception:
        return None  # degrade to Hipolabs rather than fail the whole search

    if raw.get("metadata", {}).get("total", 0) == 0:
        return []

    results = []
    for item in raw.get("results", []):
        alias = (item.get("school.alias") or "").strip() or None
        results.append({
            "name":    item.get("school.name", ""),
            "alias":   alias,
            "city":    item.get("school.city", ""),
            "state":   item.get("school.state", ""),
            "country": "United States",
            "domain":  _clean_domain(item.get("school.school_url", "")),
        })
    return results


def _search_hipolabs(q: str) -> list[dict]:
    try:
        resp = requests.get(_HIPOLABS_SEARCH, params={"name": q}, timeout=8)
        resp.raise_for_status()
        raw = resp.json()
    except requests.Timeout:
        raise HTTPException(504, "Institution search timed out")
    except Exception as exc:
        raise HTTPException(502, f"Institution search failed: {exc}")

    results = []
    for item in raw[:8]:
        domains = item.get("domains") or []
        web_pages = item.get("web_pages") or []
        domain = _clean_domain(domains[0] if domains else (web_pages[0] if web_pages else ""))
        results.append({
            "name":    item.get("name", ""),
            "alias":   None,
            "city":    "",  # Hipolabs doesn't provide city-level data
            "state":   item.get("state-province") or "",
            "country": item.get("country", ""),
            "domain":  domain,
        })
    return results


@router.get("/institutions")
async def search_institutions(
    request: Request,
    q: str = Query(..., min_length=2),
    source: str = Query("auto", pattern="^(auto|international)$"),
):
    """`source=international` skips College Scorecard and searches Hipolabs
    directly — the auto-fallback below only kicks in on a *zero-match*
    Scorecard result, so a query that returns some (wrong) US schools would
    otherwise never surface an international match. The frontend offers this
    as an explicit "search internationally" escape hatch in the dropdown."""
    from api import _check_rate_limit  # deferred: api.py imports this router at module load time
    _check_rate_limit(request, "institution_search", max_hits=30, window_secs=60)

    cache_key = f"institution_search:{source}:{q.strip().lower()}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if source == "international":
        results = _search_hipolabs(q)
    else:
        results = _search_scorecard(q)
        if not results:  # None (unconfigured/errored) or [] (genuine zero-match) both try Hipolabs
            results = _search_hipolabs(q)

    return cache.put(cache_key, results, ttl=_LOOKUP_CACHE_TTL)
