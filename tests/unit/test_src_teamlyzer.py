"""Teamlyzer (PT, IT-only) — replayed list + detail fixtures; the get-job 302 is simulated (headers are not recorded)."""

from pathlib import Path

import httpx

from jobradar.http.client import PoliteClient, ReplayTransport
from fakes import fixture_engine
from jobradar.models import short_hash
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.boards.teamlyzer import strip_utm
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "teamlyzer"


class _Transport(httpx.BaseTransport):
    def __init__(self):
        self.replay = ReplayTransport(FIX)

    def handle_request(self, request):
        if "/companies/get-job/" in str(request.url):
            uuid = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(302, headers={"location": f"https://ats.example.com/jobs/{uuid}?utm_source=teamlyzer&gh_jid=7"},
                                  request=request)
        return self.replay.handle_request(request)


def _run(**kw):
    spec = SourceSpec(id="teamlyzer", adapter="teamlyzer", params={"max_pages": 1, "max_details": 3}, default_country="PT")
    with PoliteClient(spec.id, transport=_Transport(), sleep=False, max_requests=50) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=fixture_engine().wants_detail, **kw)
        return list(get_adapter(spec.adapter)(ctx)), ctx


def test_parse_detail_and_apply_link():
    posts, ctx = _run()
    assert len(posts) == 3 and ctx.skipped > 0
    p = posts[0]
    assert p.title and p.company and p.countries == ["PT"] and p.cities
    assert p.url.startswith("https://pt.teamlyzer.com/companies/") and p.source_job_id.isdigit()
    assert p.workplace in ("hybrid", "remote", "onsite")
    # Portuguese wrapper line and review boilerplate are stripped
    assert len(p.description) > 300
    assert " na " + p.company + " (" not in p.description[:200] and "reviews no Teamlyzer" not in p.description
    # apply link = company ATS from the redirect header, utm stripped
    assert all(x.apply_url and x.apply_url.startswith("https://ats.example.com/jobs/") and "utm_" not in x.apply_url
               and x.apply_url.endswith("?gh_jid=7") for x in posts)


def test_known_postings_are_skipped():
    first, _ = _run()
    known = {short_hash("teamlyzer", first[1].source_job_id): "x"}
    again, ctx = _run(known=known)
    assert first[1].source_job_id not in {p.source_job_id for p in again}
    assert len(ctx.seen_known) == 1


def test_strip_utm():
    assert strip_utm("https://a.com/j?id=1&utm_source=x&utm_medium=y") == "https://a.com/j?id=1"
