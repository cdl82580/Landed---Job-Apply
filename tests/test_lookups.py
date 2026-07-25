"""Tests for /api/lookups/* — location and institution search proxies."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from routers.lookups import _clean_domain, _location_label


@pytest.fixture(autouse=True)
def _clear_lookup_cache():
    """routers/lookups.py caches via scripts/cache.py's module-level store,
    which isn't reset by conftest's per-test fixtures — clear it ourselves so
    tests don't leak cached results into each other via shared query strings."""
    from scripts import cache
    cache.invalidate()
    yield
    cache.invalidate()


def _resp(json_data):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = json_data
    r.raise_for_status = MagicMock()
    return r


NOMINATIM_PAYLOAD = [
    {
        "display_name": "Boston, Suffolk County, Massachusetts, United States",
        "address": {
            "city": "Boston", "state": "Massachusetts",
            "country": "United States", "country_code": "us",
        },
    },
    {
        "display_name": "Boston, Lincolnshire, England, United Kingdom",
        "address": {
            "city": "Boston", "state": "England",
            "country": "United Kingdom", "country_code": "gb",
        },
    },
]

SCORECARD_HIT = {
    "metadata": {"total": 1},
    "results": [{
        "id": 166027, "school.name": "Harvard University", "school.alias": None,
        "school.city": "Cambridge", "school.state": "MA",
        "school.school_url": "www.harvard.edu/",
    }],
}
SCORECARD_MISS = {"metadata": {"total": 0}, "results": []}
HIPOLABS_PAYLOAD = [{
    "name": "University of Oxford", "country": "United Kingdom",
    "domains": ["ox.ac.uk"], "web_pages": ["http://www.ox.ac.uk/"],
    "state-province": None,
}]


class TestLocationsEndpoint:
    def test_requires_auth(self, client):
        r = client.get("/api/lookups/locations?q=boston")
        assert r.status_code == 401

    def test_min_length_enforced(self, authed_client):
        r = authed_client.get("/api/lookups/locations?q=b")
        assert r.status_code == 422

    def test_success_normalizes_results(self, authed_client):
        with patch("routers.lookups.requests.get", return_value=_resp(NOMINATIM_PAYLOAD)):
            r = authed_client.get("/api/lookups/locations?q=boston")
        assert r.status_code == 200
        d = r.json()
        assert len(d) == 2
        assert d[0]["label"] == "Boston, Massachusetts"
        assert d[0]["city"] == "Boston"
        assert d[0]["country_code"] == "us"
        assert d[1]["label"] == "Boston, England, United Kingdom"

    def test_results_are_cached(self, authed_client):
        mock_get = MagicMock(return_value=_resp(NOMINATIM_PAYLOAD))
        with patch("routers.lookups.requests.get", mock_get):
            authed_client.get("/api/lookups/locations?q=boston")
            authed_client.get("/api/lookups/locations?q=boston")
        assert mock_get.call_count == 1

    def test_timeout_returns_504(self, authed_client):
        with patch("routers.lookups.requests.get", side_effect=requests.Timeout()):
            r = authed_client.get("/api/lookups/locations?q=boston")
        assert r.status_code == 504

    def test_upstream_error_returns_502(self, authed_client):
        with patch("routers.lookups.requests.get", side_effect=RuntimeError("boom")):
            r = authed_client.get("/api/lookups/locations?q=boston")
        assert r.status_code == 502


class TestInstitutionsEndpoint:
    def test_requires_auth(self, client):
        r = client.get("/api/lookups/institutions?q=harvard")
        assert r.status_code == 401

    def test_min_length_enforced(self, authed_client):
        r = authed_client.get("/api/lookups/institutions?q=h")
        assert r.status_code == 422

    def test_scorecard_hit_does_not_call_hipolabs(self, authed_client, monkeypatch):
        monkeypatch.setattr("routers.lookups._SCORECARD_KEY", "test-key")
        mock_get = MagicMock(return_value=_resp(SCORECARD_HIT))
        with patch("routers.lookups.requests.get", mock_get):
            r = authed_client.get("/api/lookups/institutions?q=harvard")
        assert r.status_code == 200
        d = r.json()
        assert d == [{
            "name": "Harvard University", "alias": None, "city": "Cambridge",
            "state": "MA", "country": "United States", "domain": "harvard.edu",
        }]
        assert mock_get.call_count == 1  # only Scorecard, never fell through to Hipolabs
        assert "collegescorecard" in mock_get.call_args.args[0]

    def test_scorecard_miss_falls_back_to_hipolabs(self, authed_client, monkeypatch):
        monkeypatch.setattr("routers.lookups._SCORECARD_KEY", "test-key")
        responses = [_resp(SCORECARD_MISS), _resp(HIPOLABS_PAYLOAD)]
        with patch("routers.lookups.requests.get", side_effect=responses) as mock_get:
            r = authed_client.get("/api/lookups/institutions?q=oxford")
        assert r.status_code == 200
        d = r.json()
        assert len(d) == 1
        assert d[0]["name"] == "University of Oxford"
        assert d[0]["domain"] == "ox.ac.uk"
        assert d[0]["country"] == "United Kingdom"
        assert d[0]["city"] == ""
        assert mock_get.call_count == 2

    def test_missing_key_skips_straight_to_hipolabs(self, authed_client, monkeypatch):
        monkeypatch.setattr("routers.lookups._SCORECARD_KEY", "")
        mock_get = MagicMock(return_value=_resp(HIPOLABS_PAYLOAD))
        with patch("routers.lookups.requests.get", mock_get):
            r = authed_client.get("/api/lookups/institutions?q=oxford")
        assert r.status_code == 200
        assert mock_get.call_count == 1
        assert "hipolabs" in mock_get.call_args.args[0]

    def test_results_are_cached(self, authed_client, monkeypatch):
        monkeypatch.setattr("routers.lookups._SCORECARD_KEY", "test-key")
        mock_get = MagicMock(return_value=_resp(SCORECARD_HIT))
        with patch("routers.lookups.requests.get", mock_get):
            authed_client.get("/api/lookups/institutions?q=harvard")
            authed_client.get("/api/lookups/institutions?q=harvard")
        assert mock_get.call_count == 1

    def test_hipolabs_timeout_returns_504(self, authed_client, monkeypatch):
        monkeypatch.setattr("routers.lookups._SCORECARD_KEY", "")
        with patch("routers.lookups.requests.get", side_effect=requests.Timeout()):
            r = authed_client.get("/api/lookups/institutions?q=harvard")
        assert r.status_code == 504


class TestHelpers:
    def test_clean_domain_strips_protocol_www_and_path(self):
        assert _clean_domain("www.harvard.edu/") == "harvard.edu"
        assert _clean_domain("https://www.harvard.edu/admissions") == "harvard.edu"
        assert _clean_domain("http://oxford.ac.uk") == "oxford.ac.uk"
        assert _clean_domain("") == ""

    def test_location_label_us_uses_state(self):
        addr = {"city": "Boston", "state": "Massachusetts", "country": "United States", "country_code": "us"}
        label, city, state, country, cc = _location_label(addr, "Boston, ...")
        assert label == "Boston, Massachusetts"

    def test_location_label_international_uses_country(self):
        addr = {"city": "Oxford", "country": "United Kingdom", "country_code": "gb"}
        label, *_ = _location_label(addr, "Oxford, ...")
        assert label == "Oxford, United Kingdom"

    def test_location_label_falls_back_to_display_name(self):
        addr = {"country_code": "us"}
        label, city, *_ = _location_label(addr, "Somewhere, Some County, ST")
        assert city == "Somewhere"
