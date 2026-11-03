from datetime import datetime, timezone
from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.match.engine import MatchEngine
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.boards.karriere import german_date, parse_salary
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "karriere_at"


def test_parse():
    spec = SourceSpec(id="karriere_at", adapter="karriere_at", default_country="AT",
                      params={"keywords": ["data engineer"], "max_pages": 2, "max_details": 2})
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=50) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=MatchEngine.load().wants_detail)
        posts = list(get_adapter(spec.adapter)(ctx))
    assert len(posts) >= 25 and all(p.title and p.url and p.company for p in posts)
    assert len({p.source_job_id for p in posts}) == len(posts)
    assert all(p.url.startswith("https://www.karriere.at/jobs/") and p.posted_at for p in posts)
    assert any(p.workplace == "hybrid" and "homeoffice" in p.tags for p in posts)
    assert any(p.salary_min and p.salary_currency == "EUR" and p.salary_period in ("month", "year") for p in posts)
    detailed = [p for p in posts if p.countries == ["AT"]]  # JSON-LD addressCountry only on detail pages
    assert len(detailed) == 2 and all(len(p.description) > 1000 for p in detailed)


def test_helpers():
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    assert german_date("Heute veröffentlicht", now) == now
    assert german_date("vor 3 Tagen veröffentlicht", now).day == 19
    assert german_date("13.10.2026").day == 13
    assert parse_salary("3.500 € – 4.500 € monatlich") == (3500.0, 4500.0, "EUR", "month")
    assert parse_salary("ab 51.947,84 € jährlich") == (51947.84, None, "EUR", "year")
    assert parse_salary("nach Vereinbarung") == (None, None, None, None)
