from pathlib import Path

import pytest

from jobradar.http.client import PoliteClient, ReplayTransport
from fakes import fixture_engine
from jobradar.models import short_hash
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIXTURES = Path(__file__).parents[1] / "fixtures"
SITES = {
    "cv_ee": ("www.cv.ee", "EE", 3),
    "cv_lv": ("www.cv.lv", "LV", 2),
    "cvonline_lt": ("www.cvonline.lt", "LT", 2),
}


def _run(sid, **kw):
    host, cc, n = SITES[sid]
    spec = SourceSpec(id=sid, adapter="cvonline", params={"host": host, "country": cc, "max_details": n},
                      default_country=cc)
    with PoliteClient(sid, transport=ReplayTransport(FIXTURES / sid), sleep=False, max_requests=100) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=fixture_engine().wants_detail, **kw)
        return list(get_adapter("cvonline")(ctx)), ctx


@pytest.mark.parametrize("sid", list(SITES))
def test_parse_each_site(sid):
    posts, ctx = _run(sid)
    assert len(posts) == SITES[sid][2] and ctx.skipped >= 3
    cc = SITES[sid][1]
    for p in posts:
        assert p.title and p.company and p.countries == [cc] and p.posted_at
        assert f"/en/vacancy/{p.source_job_id}/" in p.url and "<" not in p.description
    assert any(len(p.description) > 500 for p in posts)  # IFRAME vacancies (SEB on Lever) carry no text
    assert all(p.apply_url for p in posts if not p.description)
    assert all(p.workplace in ("hybrid", "remote", "onsite", None) for p in posts)


def test_cv_ee_fully_parsed_posting():
    posts, _ = _run("cv_ee")
    p = next(x for x in posts if x.title == "AI Engineer")
    assert p.company == "Playtech Estonia OÜ" and p.workplace == "hybrid" and p.cities == ["Tallinn"]
    assert p.apply_url == "https://easyapply.jobs/r/5KmVeS528twcF54MWUvh"
    assert len(p.description) > 1000


def test_company_ats_apply_links():
    posts, _ = _run("cv_lv")
    assert any("myworkdayjobs.com" in (p.apply_url or "") for p in posts)
    posts, _ = _run("cvonline_lt")
    assert any("lever.co" in (p.apply_url or "") for p in posts)


def test_known_postings_skip_detail():
    first, _ = _run("cv_ee")
    known = {short_hash("cv_ee", p.source_job_id): "x" for p in first}
    again, ctx = _run("cv_ee", known=known)
    assert len(again) == 0 and len(ctx.seen_known) == len(first)
