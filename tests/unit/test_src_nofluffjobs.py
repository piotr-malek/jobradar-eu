"""NoFluffJobs: SSR listing state (serverApp-state) → title gate → offer-page JSON-LD (offline replay).

Fixtures recorded live 2026-10-22; the listing state was trimmed to 23 postings (3 title-relevant ones
whose offer pages were recorded + 20 title-excluded), offer pages reduced to JSON-LD + the /posting/ state.
"""

import re
from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.models import short_hash
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "nofluffjobs"
LISTING = "https://nofluffjobs.com/?criteria=category%3Ddata,business-intelligence,artificial-intelligence&page={page}"
GATE = re.compile(r"\b(?:data|database|bi|etl|analytics|ai)\b", re.I)


def _run(known=None, **params):
    spec = SourceSpec(id="nofluffjobs", adapter="nofluffjobs", default_country="PL",
                      params={"listings": [LISTING], "pages": 10, "max_details": 30, **params})
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=100,
                      respect_robots=True, browser=True) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=lambda t: bool(GATE.search(t)), known=known or {})
        posts = list(get_adapter(spec.adapter)(ctx))
        return posts, ctx, http.stats


def test_listing_and_details():
    posts, ctx, stats = _run()
    assert "404" not in stats.status_counts
    assert len(posts) == 23
    assert all(p.title and p.company and p.url.startswith("https://nofluffjobs.com/job/") for p in posts)
    assert all(p.workplace in ("remote", "hybrid", "onsite") for p in posts)
    assert all(p.posted_at for p in posts)
    detailed = [p for p in posts if p.description]
    assert len(detailed) == 3
    angry = next(p for p in posts if p.company == "Angry Nerds")
    assert angry.workplace == "remote" and angry.countries == ["PL"]
    assert angry.salary_currency == "PLN" and angry.salary_period and angry.salary_min
    assert len(angry.description) > 300
    mind = next(p for p in posts if p.title.startswith("Power BI"))
    assert mind.workplace == "hybrid" and mind.cities == ["Katowice"]
    assert (mind.salary_period, mind.salary_min, mind.salary_max) == ("day", 1100.0, 1400.0)
    xeb = next(p for p in posts if "(Warsaw)" in p.title)
    assert xeb.workplace == "onsite"
    assert any("details_fetched=3" in n for n in ctx.notes)


def test_known_relevant_postings_are_skipped():
    posts, _, stats = _run()
    angry = next(p for p in posts if p.company == "Angry Nerds")
    posts2, ctx2, stats2 = _run(known={short_hash("nofluffjobs", angry.source_job_id): "v"})
    assert len(posts2) == 22 and stats2.requests == stats.requests - 1


def test_list_only():
    posts, _, stats = _run(list_only=True)
    assert len(posts) == 23 and not any(p.description for p in posts)
    assert stats.requests == 2  # robots + listing
