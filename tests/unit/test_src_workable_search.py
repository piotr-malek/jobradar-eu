"""Workable cross-company search adapter — replayed fixtures."""

from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "workable_search"


def test_parse():
    spec = SourceSpec(id="workable_search", adapter="workable_search",
                      params={"locations": ["Europe", "Poland"], "queries": ["data engineer"], "max_pages": 1})
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=50) as http:
        posts = list(get_adapter(spec.adapter)(FetchContext(spec=spec, http=http)))
        assert http.stats.requests == 2
    assert len(posts) >= 20 and all(p.title and p.company for p in posts)
    assert all(p.url.startswith("https://jobs.workable.com/view/") for p in posts)
    assert len({p.source_job_id for p in posts}) == len(posts)  # same job in Europe and Poland slices is yielded once
    assert all(p.description for p in posts)
    assert {p.workplace for p in posts} <= {"remote", "hybrid", "onsite", None}
    assert all(len(c) == 2 for p in posts for c in p.countries)
    assert all(p.ats_type is None for p in posts)  # URL has no account slug / shortcode
