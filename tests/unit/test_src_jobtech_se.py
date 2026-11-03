"""JobTech JobSearch (Arbetsförmedlingen) adapter — replayed fixtures."""

from datetime import timezone
from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.boards.national_apis import local_date
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "jobtech_se"


def test_parse():
    spec = SourceSpec(id="jobtech_se", adapter="jobtech_se", params={"published_within_days": 7}, default_country="SE")
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=50) as http:
        posts = list(get_adapter(spec.adapter)(FetchContext(spec=spec, http=http)))
    assert posts and all(p.title and p.url for p in posts)
    assert len({p.source_job_id for p in posts}) == len(posts)
    assert all(p.description and len(p.description) > 100 for p in posts)
    assert {c for p in posts for c in p.countries} <= {"SE", "CH", "NO", "DK", "FI", "DE"}
    assert {p.workplace for p in posts} <= {None, "remote", "hybrid", "onsite"}
    assert all(p.posted_at and p.posted_at.tzinfo for p in posts)
    assert http.stats.requests == 6  # one per default query


def test_local_time_is_stockholm():
    d = local_date("2026-10-22T16:07:20", "Europe/Stockholm")
    assert d.astimezone(timezone.utc).hour == 14  # CEST = UTC+2
