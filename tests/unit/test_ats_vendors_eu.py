"""Parsers for Softgarden, Traffit, Homerun and JOIN (small recorded fixtures in tests/fixtures/ats_vendors)."""
from __future__ import annotations

from pathlib import Path

import httpx

from jobradar.http.client import PoliteClient
from jobradar.sources.ats.vendors import FETCHERS, fetch_homerun, fetch_join, fetch_softgarden, fetch_traffit

FIX = Path(__file__).parent.parent / "fixtures" / "ats_vendors"


def _txt(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def _client(handler):
    return PoliteClient("t", transport=httpx.MockTransport(handler), sleep=False)


def test_registered():
    assert {"softgarden", "traffit", "homerun", "join"} <= set(FETCHERS)


def test_softgarden_list_and_detail():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if "/job/" in req.url.path:
            return httpx.Response(200, text=_txt("softgarden_job.html"))
        return httpx.Response(200, text=_txt("softgarden_list.html"))

    with _client(handler) as http:
        code, posts = fetch_softgarden(http, "acme", {}, lambda t: False)
        assert code == 200 and len(posts) == 2 and len(seen) == 1
        assert posts[0].location_raw == "Hamburg" and posts[0].posted_at.year == 2026
        assert posts[0].url.startswith("https://acme.softgarden.io/job/") and posts[0].description == ""
        code, posts = fetch_softgarden(http, "acme", {}, lambda t: "Ergo" in t)
        assert [bool(p.description) for p in posts] == [False, True] and posts[1].countries == []


def test_softgarden_dead_slug():
    with _client(lambda req: httpx.Response(404)) as http:
        assert fetch_softgarden(http, "nope", {}, lambda t: True) == (404, [])


def test_traffit_paginates_with_headers():
    import json
    rows = json.loads(_txt("traffit_published.json"))
    calls = []

    def handler(req):
        page = req.headers.get("x-request-current-page")
        calls.append((page, req.headers.get("x-request-page-size")))
        return httpx.Response(200, json=rows[:1] if page == "1" else rows[1:], headers={"x-result-total-pages": "2"})

    with _client(handler) as http:
        code, posts = fetch_traffit(http, "antalpl", {}, lambda t: False)
    assert code == 200 and len(posts) == 2 and calls == [("1", "100"), ("2", "100")]
    p = posts[0]
    assert p.title and p.location_raw and p.countries == ["PL"] and p.description and p.posted_at and p.url.startswith("https://")


def test_homerun_atom():
    with _client(lambda req: httpx.Response(200, text=_txt("homerun_feed.xml"))) as http:
        code, posts = fetch_homerun(http, "2manydots", {}, lambda t: False)
        assert code == 200 and len(posts) == 1
        p = posts[0]
        assert p.title == "Web Analyst" and p.location_raw == "Warsaw, Poland" and "data expert" in p.description
        assert p.url == "https://2manydots.homerun.co/tracking-specialist" and p.company == "2manydots" and p.tags == ["Data"]
    with _client(lambda req: httpx.Response(404, text="<html/>")) as http:
        assert fetch_homerun(http, "nope", {}, lambda t: True) == (404, [])


def test_join_company_page_and_detail():
    def handler(req):
        if "/companies/take5nl/" in req.url.path:
            return httpx.Response(200, text=_txt("join_job.html"))
        return httpx.Response(200, text=_txt("join_company.html"))

    with _client(handler) as http:
        code, posts = fetch_join(http, "take5nl", {}, lambda t: False)
        assert code == 200 and len(posts) == 2 and posts[0].workplace == "hybrid" and posts[0].countries == ["NL"]
        assert posts[0].location_raw == "Bergen op Zoom, Netherlands" and posts[0].company == "Take 5"
        assert posts[0].url == "https://join.com/companies/take5nl/16831134-junior-adviseur-bedrijfsovernames"
        code, posts = fetch_join(http, "take5nl", {}, lambda t: True)
        assert posts[0].description
