import base64
from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from fakes import fixture_engine
from jobradar.models import short_hash
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.boards.cvlt import decode_apply
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "cv_lt"


def _run(**kw):
    spec = SourceSpec(id="cv_lt", adapter="cvlt", params={"max_details": 3}, default_country="LT")
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=100) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=fixture_engine().wants_detail, **kw)
        return list(get_adapter("cvlt")(ctx)), ctx


def test_parse_posting():
    posts, ctx = _run()
    assert len(posts) == 3 and ctx.skipped >= 3
    p = next(x for x in posts if x.title.startswith("Analytics Engineer"))
    assert p.company == "Wargaming Vilnius, UAB" and p.countries == ["LT"] and p.cities == ["Vilnius"]
    assert p.workplace == "hybrid" and p.posted_at and len(p.description) > 500 and "<" not in p.description
    assert (p.salary_min, p.salary_max, p.salary_period) == (4000.0, 6500.0, "month")
    assert p.apply_url == "https://wargaming.com/en/careers/vacancy_3560058_vilnius/"
    assert {x.workplace for x in posts} >= {"hybrid", "remote"}


def test_decode_apply_without_request():
    enc = base64.urlsafe_b64encode(b"https://jobs.lever.co/acme/123").decode().rstrip("=")
    assert decode_apply(f'href="/jobs/1/apply-2-ats?redirect={enc}"') == "https://jobs.lever.co/acme/123"
    assert decode_apply("no button") is None


def test_known_postings_skip_detail():
    first, _ = _run()
    known = {short_hash("cv_lt", p.source_job_id): "x" for p in first}
    again, ctx = _run(known=known)
    assert again == [] and len(ctx.seen_known) == 3
