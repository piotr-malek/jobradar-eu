"""Bundesagentur für Arbeit Jobsuche adapter — replayed from recorded fixtures (2026-10-22)."""

from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "arbeitsagentur"
PARAMS = {"size": 50, "max_pages": 1, "max_details": 2, "published_within_days": 7}


def _run(params=PARAMS):
    spec = SourceSpec(id="arbeitsagentur", adapter="arbeitsagentur", params=params, default_country="DE")
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=50) as http:
        ctx = FetchContext(spec=spec, http=http)
        return list(get_adapter(spec.adapter)(ctx)), ctx, http


def test_parse():
    posts, ctx, http = _run()
    assert len(posts) > 20 and all(p.title and p.url for p in posts)
    assert all(p.source == "arbeitsagentur" for p in posts)
    assert len({p.source_job_id for p in posts}) == len(posts)
    assert all(p.url.startswith("https://www.arbeitsagentur.de/jobsuche/jobdetail/") for p in posts)
    assert all(c.isalpha() and len(c) == 2 and c.isupper() for p in posts for c in p.countries)
    assert {"DE"} <= {c for p in posts for c in p.countries}
    assert all(p.posted_at is not None for p in posts)
    # homeoffice filter → every posting allows home office; 100 % → remote
    assert {p.workplace for p in posts} <= {"remote", "hybrid"}
    assert any(p.workplace == "remote" for p in posts)
    # details (v4) fetched for the first two gated titles only
    with_desc = [p for p in posts if p.description]
    assert len(with_desc) == 2 and all("<" not in p.description[:500] for p in with_desc)
    assert http.stats.requests == 7  # 5 searches + 2 details


def test_salary_and_types():
    posts, _, _ = _run()
    p = next(p for p in posts if p.source_job_id == "18024-10Q9A00000X0A-S")
    assert (p.salary_min, p.salary_max, p.salary_currency, p.salary_period) == (53400.0, 63200.0, "EUR", "year")
    assert p.company == "Volksbank Mittweida eG" and p.cities == ["Mittweida"]
    assert "full_time" in p.employment_types


def test_no_details_when_disabled():
    posts, _, http = _run({**PARAMS, "fetch_details": False})
    assert posts and not any(p.description for p in posts)
    assert http.stats.requests == 5
