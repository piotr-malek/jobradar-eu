from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from fakes import fixture_engine
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "startupjobs_cz"


def test_parse():
    spec = SourceSpec(id="startupjobs_cz", adapter="startupjobs_cz", default_country="CZ",
                      params={"queries": ["data"], "max_pages": 1, "max_details": 2})
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=50) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=fixture_engine().wants_detail)
        posts = list(get_adapter(spec.adapter)(ctx))
    assert len(posts) == 20 and all(p.title and p.url and p.company for p in posts)
    assert all(p.url.startswith("https://www.startupjobs.cz/nabidka/") for p in posts)
    assert {p.workplace for p in posts} <= {"remote", "hybrid", "onsite"}
    assert all(c == "CZ" for p in posts for c in p.countries)
    detailed = [p for p in posts if p.description]
    assert len(detailed) == 2 and all(p.posted_at and "<" not in p.description for p in detailed)
    de = next(p for p in detailed if p.title == "Data Engineer")
    assert "Required languages: Czech (native)" in de.description and "Teradata" in de.tags
