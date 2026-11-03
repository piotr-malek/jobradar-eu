from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from fakes import fixture_engine
from jobradar.models import short_hash
from jobradar.normalize.lang import language_requirements
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "ictjob_be"


def _run(**kw):
    spec = SourceSpec(id="ictjob_be", adapter="ictjob_be", params={}, default_country="BE")
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=100) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=fixture_engine().wants_detail, **kw)
        return list(get_adapter(spec.adapter)(ctx)), ctx


def test_parse_and_dedupe():
    posts, ctx = _run()
    assert posts and ctx.skipped > 0
    assert len({p.source_job_id for p in posts}) == len(posts)
    assert all(p.url.startswith("https://www.ictjob.be/en/job/") and "?" not in p.url for p in posts)
    assert all(p.title and p.company and p.cities for p in posts)
    # details merged: HTML body as text, work arrangement, posting date
    assert all("<" not in p.description and len(p.description) > 300 for p in posts)
    assert any(p.workplace == "hybrid" for p in posts) and all(p.posted_at for p in posts)


def test_language_requirements_reach_description():
    posts, _ = _run()
    req = [language_requirements(p.description) for p in posts]
    assert any(r.get("nl") == "required" for r in req)
    assert any(r.get("fr") == "required" for r in req)
    assert any(r.get("nl") == "plus" for r in req)  # "basic" level or English-alternative → plus, not required


def test_known_postings_skip_detail():
    first, _ = _run()
    known = {short_hash("ictjob_be", p.source_job_id): "x" for p in first}
    again, ctx = _run(known=known)
    assert again == [] and len(ctx.seen_known) == len(first)
