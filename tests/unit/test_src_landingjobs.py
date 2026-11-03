"""Landing.jobs adapter (sitemap + JSON-LD mode) — replayed, trimmed fixtures."""

from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.boards.public_boards import _slug_title
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "landingjobs"
GATE = {"senior data engineer", "ai solutions consultant", "analytics engineer"}


def test_parse():
    spec = SourceSpec(id="landingjobs", adapter="landingjobs", params={"max_details": 3})
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=50) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=lambda t: t in GATE)
        posts = list(get_adapter(spec.adapter)(ctx))
        assert http.stats.requests == 4  # sitemap + 3 job pages; non-data slugs never fetched
    assert [p.source_job_id for p in posts] == [
        "inscale/senior-data-engineer-in-lisbon-2026-9", "damia/ai-solutions-consultant",
        "dashlane-pt/analytics-engineer-in-lisbon"]
    p = posts[0]
    assert p.title == "Senior Data Engineer" and p.countries == ["PT"] and p.cities == ["Lisbon"]
    assert (p.salary_min, p.salary_max, p.salary_currency, p.salary_period) == (60200.0, 74200.0, "EUR", "year")
    assert all(p.description and p.posted_at for p in posts)


def test_slug_title():
    assert _slug_title("senior-data-engineer-in-lisbon-2026-9") == "senior data engineer"
    assert _slug_title("analytics-engineer-in-lisbon") == "analytics engineer"
