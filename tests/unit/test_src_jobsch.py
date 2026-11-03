from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.match.engine import MatchEngine
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "jobsch"


def test_parse():
    spec = SourceSpec(id="jobsch", adapter="jobsch", default_country="CH",
                      params={"queries": ["data engineer"], "max_pages": 1, "max_details": 3})
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=50) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=MatchEngine.load().wants_detail)
        posts = list(get_adapter(spec.adapter)(ctx))
    assert len(posts) >= 20 and all(p.title and p.url and p.company for p in posts)
    assert all(p.url.startswith("https://www.jobs.ch/en/vacancies/detail/") for p in posts)
    assert all(p.countries == ["CH"] and p.posted_at for p in posts)
    detailed = [p for p in posts if p.description]
    assert len(detailed) == 3  # max_details; list stubs carry only boilerplate text
    assert all(len(p.description) > 300 and "<" not in p.description and p.location_raw for p in detailed)
