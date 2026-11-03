"""Get on Board public API adapter — replayed fixtures."""

from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "getonbrd"


def test_parse():
    spec = SourceSpec(id="getonbrd", adapter="getonbrd",       # the queries the fixtures were recorded with
                      params={"max_pages": 1, "queries": ["data engineer", "analytics engineer", "BI developer", "ETL", "dbt"]})
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=50) as http:
        posts = list(get_adapter(spec.adapter)(FetchContext(spec=spec, http=http)))
        assert http.stats.requests == 5
    assert len(posts) >= 10 and all(p.title and p.url.startswith("https://www.getonbrd.com/jobs/") for p in posts)
    assert len({p.source_job_id for p in posts}) == len(posts)
    assert {p.workplace for p in posts} <= {"remote", "hybrid", "onsite", None}
    assert all(len(c) == 2 for p in posts for c in p.countries)
    local = [p for p in posts if p.raw["remote_modality"] == "remote_local"]
    # remote for residents of the listed countries; some live rows list none (then scope stays unknown)
    assert local and all(p.remote_scope and p.countries for p in local if p.raw["tenants"])
    assert all(p.company for p in posts)
    assert all(p.description for p in posts)
