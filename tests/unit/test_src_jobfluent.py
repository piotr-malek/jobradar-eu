"""Jobfluent (ES) — replayed city sitemaps and server-rendered detail pages."""

from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from fakes import fixture_engine
from jobradar.models import short_hash
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.boards.jobfluent import slug_title
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "jobfluent"
PARAMS = {"sitemaps": ["remote", "barcelona"], "max_details": 3}


def _run(**kw):
    spec = SourceSpec(id="jobfluent", adapter="jobfluent", params=PARAMS, default_country="ES")
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=50) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=fixture_engine().wants_detail, **kw)
        return list(get_adapter(spec.adapter)(ctx)), ctx


def test_parse_posting_and_dedupe():
    posts, ctx = _run()
    assert len(posts) == 3 and ctx.skipped > 100
    assert len({p.source_job_id for p in posts}) == 3  # remote jobs repeated in the city sitemap count once
    p = posts[0]
    assert p.title and p.company and p.countries == ["ES"] and p.cities
    assert p.workplace in ("remote", "hybrid", "onsite") and p.tags
    assert len(p.description) > 300 and "<" not in p.description
    assert p.url.startswith("https://www.jobfluent.com/jobs/") and p.apply_url is None


def test_known_postings_skip_detail():
    first, _ = _run()
    known = {short_hash("jobfluent", p.source_job_id): "x" for p in first}
    again, ctx = _run(known=known)
    assert all(p.source_job_id not in {f.source_job_id for f in first} for p in again)
    assert len(ctx.seen_known) == len(first)


def test_slug_title_drops_city():
    assert slug_title("senior-data-engineer-barcelona") == "senior data engineer"
