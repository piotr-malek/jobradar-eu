"""Jobgether agents API adapter — replayed fixtures."""

from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "jobgether"


def test_parse():
    spec = SourceSpec(id="jobgether", adapter="jobgether",
                      params={"locations": ["europe", "poland"], "keywords": ["data engineer"], "max_pages": 1})
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=50) as http:
        posts = list(get_adapter(spec.adapter)(FetchContext(spec=spec, http=http)))
        assert http.stats.requests == 2
    assert len(posts) >= 20 and all(p.title and p.company for p in posts)
    assert all(p.url.startswith("https://jobgether.com/offer/") for p in posts)
    assert len({p.source_job_id for p in posts}) == len(posts)
    assert len({(p.company.casefold(), p.title.casefold()) for p in posts}) == len(posts)  # cross-city dupes dropped
    assert {p.workplace for p in posts} <= {"remote", "hybrid", None}
    assert all(len(c) == 2 for p in posts for c in p.countries)
