"""Aggregator adapters (Jooble, open-jobs-data, LinkedIn guest, JobSpy Indeed) — replayed fixtures."""

import json
import sys
import types
from pathlib import Path

import pytest

from jobradar.http.client import Blocked, PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.boards.aggregators import SourceError
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures"


def _run(spec, fixture_dir, **ctx_kw):
    with PoliteClient(spec.id, transport=ReplayTransport(fixture_dir), sleep=False, max_requests=50) as http:
        posts = list(get_adapter(spec.adapter)(FetchContext(spec=spec, http=http, **ctx_kw)))
        return posts, http.stats.requests


def test_jooble():
    spec = SourceSpec(id="jooble", adapter="jooble", secrets=["JOOBLE_PL_API_KEY"],
                      params={"keywords": ["data engineer"], "locations": ["Poland", "Germany", "Remote"], "per_page": 8})
    posts, requests = _run(spec, FIX / "jooble", secrets={"JOOBLE_PL_API_KEY": "TESTKEY"})
    assert requests == 3  # one page per location; a short page stops paging
    assert len(posts) == len({p.source_job_id for p in posts}) == 15
    assert all(p.title and p.url.startswith("https://jooble.org/") and p.posted_at for p in posts)
    assert {p.raw["query"]["location"] for p in posts} == {"Poland", "Germany", "Remote"}


def test_jooble_remote_only_keeps_results_with_a_remote_word():
    from jobradar.normalize.location import mentions_remote

    spec = SourceSpec(id="jooble", adapter="jooble", secrets=["JOOBLE_PL_API_KEY"],
                      params={"keywords": ["data engineer"], "locations": ["Poland", "Germany", "Remote"], "per_page": 8,
                              "remote_only": True})
    with PoliteClient(spec.id, transport=ReplayTransport(FIX / "jooble"), sleep=False, max_requests=50) as http:
        ctx = FetchContext(spec=spec, http=http, secrets={"JOOBLE_PL_API_KEY": "TESTKEY"})
        posts = list(get_adapter(spec.adapter)(ctx))
    assert 0 < len(posts) < 15 and len(posts) + ctx.skipped == 15
    assert all(mentions_remote(f"{p.title} | {p.location_raw} | {p.description}") for p in posts)
    for text in ("Teletrabajo", "Home office", "Homeoffice", "Thuiswerken", "Data Engineer | 100% Remoto", "Teletrabalho"):
        assert mentions_remote(text), text
    assert not mentions_remote("Data Engineer | Madrid | hybrid cloud on GCP")
    assert not mentions_remote("RevOps Analyst | Casares, Málaga | businesses sell anywhere and deliver everywhere")


def test_jooble_without_key_fails_cleanly():
    spec = SourceSpec(id="jooble_de", adapter="jooble", secrets=["JOOBLE_DE_API_KEY"])
    with pytest.raises(SourceError, match="JOOBLE_DE_API_KEY missing"):
        _run(spec, FIX / "jooble", secrets={"JOOBLE_PL_API_KEY": "OTHER"})  # another site's key is not used


def test_open_jobs_data():
    posts, requests = _run(SourceSpec(id="open_jobs_data", adapter="open_jobs_data"), FIX / "open_jobs_data")
    assert requests == 2  # new-jobs.json + companies.json (slug lookup for custom career domains)
    assert len(posts) == len({p.source_job_id for p in posts}) == 25
    assert all(p.title and p.company and p.url.startswith("https://") for p in posts)
    assert all(":" in p.source_job_id for p in posts)  # <platform>:<jobId>
    assert any(p.workplace == "remote" for p in posts)


def test_linkedin_guest_refuses_without_permission():
    # robots.txt disallows /jobs-guest/ → the adapter refuses before any search request
    spec = SourceSpec(id="linkedin_guest", adapter="linkedin_guest")
    with pytest.raises(Blocked):
        _run(spec, FIX / "linkedin_guest" / "robots")


def test_linkedin_guest_parses_cards():
    spec = SourceSpec(id="linkedin_guest", adapter="linkedin_guest",
                      params={"permission_granted": True, "max_searches": 1})
    posts, requests = _run(spec, FIX / "linkedin_guest")
    assert requests == 1
    assert [p.source_job_id for p in posts] == ["4100000001", "4100000002", "4100000003"]
    assert all(p.title and p.company and p.posted_at and p.workplace == "remote" for p in posts)


def test_jobspy_indeed(monkeypatch):
    recorded = json.loads((FIX / "jobspy_indeed" / "records.json").read_text(encoding="utf-8"))

    class FakeFrame:
        def __init__(self, rows):
            self.rows = rows

        def to_dict(self, orient):
            assert orient == "records"
            return self.rows

    calls = []

    def scrape_jobs(**kw):
        calls.append(kw)
        for rec in recorded:
            if rec["kwargs"]["search_term"] == kw["search_term"]:
                return FakeFrame(rec["records"])
        return FakeFrame([])

    monkeypatch.setitem(sys.modules, "jobspy", types.SimpleNamespace(scrape_jobs=scrape_jobs))
    term = recorded[0]["kwargs"]["search_term"]
    spec = SourceSpec(id="jobspy_indeed", adapter="jobspy_indeed",
                      params={"countries": ["poland"], "search_terms": [term], "delay_s": 0})
    posts, requests = _run(spec, FIX / "jobspy_indeed")
    assert requests == 0 and len(calls) == 1  # jobspy does its own HTTP
    assert calls[0]["site_name"] == ["indeed"] and calls[0]["country_indeed"] == "poland"
    assert posts and len(posts) == len({p.source_job_id for p in posts})
    assert all(p.title and p.url and p.countries == ["PL"] for p in posts)


def test_adzuna_asks_only_its_own_countries_and_keeps_the_redirect_link():
    import httpx

    asked = []

    def handler(request):
        asked.append(request.url.path)
        return httpx.Response(200, json={"results": [{
            "id": 77, "title": "<strong>Backend</strong> Engineer", "company": {"display_name": "Acme"},
            "redirect_url": "https://www.adzuna.at/land/ad/77", "location": {"display_name": "Wien",
                                                                            "area": ["Österreich", "Wien", "Wien"]},
            "description": "Python and Go.", "created": "2026-11-07T10:00:00Z", "salary_is_predicted": "1",
            "salary_min": 50000}]})

    spec = SourceSpec(id="adzuna", adapter="adzuna", secrets=["ADZUNA_APP_ID", "ADZUNA_APP_KEY"],
                      params={"countries": ["bg", "at"], "queries": ["backend engineer"]})
    with PoliteClient(spec.id, transport=httpx.MockTransport(handler), sleep=False, max_requests=5) as http:
        posts = list(get_adapter("adzuna")(FetchContext(spec=spec, http=http,
                                                        secrets={"ADZUNA_APP_ID": "i", "ADZUNA_APP_KEY": "k"})))
    assert asked == ["/v1/api/jobs/at/search/1"]                  # no Adzuna site for Bulgaria
    p, = posts
    assert (p.title, p.company, p.countries, p.cities) == ("Backend Engineer", "Acme", ["AT"], ["Wien"])
    assert p.url == "https://www.adzuna.at/land/ad/77" and p.salary_min is None     # predicted salaries dropped
