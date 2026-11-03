"""TheirStack: the free plan refuses more than 25 results per page (403 E-020), so the adapter pages."""

from __future__ import annotations

import json

import httpx
import pytest

from jobradar.http.client import Blocked, PoliteClient
from jobradar.sources.registry import get_adapter
from jobradar.sources.base import FetchContext, SourceSpec

LIMIT_ERROR = {"error": {"code": "E-020", "title": "Premium functionality limitation",
                         "description": "The free plan does not allow more than 25 results per page."}}


def _api(available: int):
    calls: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body)
        if body["limit"] > 25:
            return httpx.Response(403, json=LIMIT_ERROR)
        start = body["page"] * body["limit"]
        ids = range(start, min(start + body["limit"], available))
        return httpx.Response(200, json={"data": [
            {"id": i, "job_title": "Analytics Engineer", "company": "Acme", "url": f"https://x/{i}",
             "country_code": "PL", "remote": True, "date_posted": "2026-11-01"} for i in ids]})

    return handler, calls


def _run(params: dict, available: int, max_requests: int = 2):
    handler, calls = _api(available)
    spec = SourceSpec(id="theirstack", adapter="theirstack", params=params, secrets=["THEIR_STACK_API_KEY"])
    with PoliteClient(spec.id, transport=httpx.MockTransport(handler), sleep=False,
                      max_requests=max_requests, retries=0) as http:
        ctx = FetchContext(spec=spec, http=http, secrets={"THEIR_STACK_API_KEY": "k"})
        posts = list(get_adapter("theirstack")(ctx))
    return posts, calls


def test_pages_within_the_free_plan_limit():
    posts, calls = _run({"limit": 40, "per_page": 20}, available=100)
    assert [(c["page"], c["limit"]) for c in calls] == [(0, 20), (1, 20)]
    assert len(posts) == 40 and len({p.source_job_id for p in posts}) == 40
    assert posts[0].workplace == "remote" and posts[0].countries == ["PL"]


def test_per_page_is_capped_at_25_and_the_total_is_kept():
    posts, calls = _run({"limit": 40, "per_page": 40}, available=100)
    assert [(c["page"], c["limit"]) for c in calls] == [(0, 25), (1, 25)]
    assert [p.source_job_id for p in posts] == [str(i) for i in range(40)]


def test_stops_after_a_short_page():
    posts, calls = _run({"limit": 40, "per_page": 20}, available=7)
    assert len(calls) == 1 and len(posts) == 7


def test_refusal_carries_the_api_reason():
    handler, _ = _api(10)
    with PoliteClient("t", transport=httpx.MockTransport(handler), sleep=False, max_requests=1, retries=0) as http:
        with pytest.raises(Blocked) as exc:
            http.post_json("https://api.theirstack.com/v1/jobs/search", json={"limit": 40, "page": 0})
    assert "E-020" in str(exc.value) and "(403)" in str(exc.value)
