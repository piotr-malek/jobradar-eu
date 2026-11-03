"""EURES job-vacancy search adapter — replayed fixtures (two POSTs to the same URL)."""

from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.boards.national_apis import _eures_body
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "eures"


def test_parse():
    spec = SourceSpec(id="eures", adapter="eures", params={"keywords": ["analytics engineer", "BI developer"], "max_pages": 1})
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=50) as http:
        posts = list(get_adapter(spec.adapter)(FetchContext(spec=spec, http=http)))
        assert http.stats.requests == 2
    assert len(posts) >= 10 and all(p.title and p.url.startswith("https://europa.eu/eures/portal/jv-se/jv-details/") for p in posts)
    assert len({p.source_job_id for p in posts}) == len(posts)
    assert all(p.countries and all(len(c) == 2 for c in p.countries) for p in posts)
    assert "EL" not in {c for p in posts for c in p.countries}
    assert sum(1 for p in posts if len(p.description) > 200) >= len(posts) * 0.8
    assert all(p.posted_at for p in posts)
    assert any("analytics engineer" in p.title.lower() for p in posts)


def test_body_shape():
    b = _eures_body("data engineer", 2, 50, "LAST_WEEK", [])
    assert b["keywords"] == [{"keyword": "data engineer", "specificSearchCode": "TITLE"}]
    assert b["sortSearch"] == "BEST_MATCH" and b["page"] == 2


def test_server_error_on_one_keyword_skips_to_the_next():
    import httpx

    from jobradar.http.client import PoliteClient
    from jobradar.sources.base import FetchContext, SourceSpec
    from jobradar.sources.registry import get_adapter

    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"jvs": [{"id": "x1", "title": "Data Engineer", "locationMap": {"PL": []},
                                                    "employer": {"name": "Acme"}, "description": "<p>SQL</p>"}]})

    spec = SourceSpec(id="eures", adapter="eures", params={"keywords": ["a", "b"], "max_pages": 1})
    with PoliteClient(spec.id, transport=httpx.MockTransport(handler), sleep=False, max_requests=10, retries=0) as http:
        ctx = FetchContext(spec=spec, http=http)
        posts = list(get_adapter("eures")(ctx))
    assert [p.source_job_id for p in posts] == ["x1"] and ctx.notes == ["HTTP 500 on keyword 1"]
