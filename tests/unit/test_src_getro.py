from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.models import short_hash
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "getro"
BOARDS = ["jobs.pointnine.com", "careers.speedinvest.com"]


def _run(*, known=None, wants=lambda t: "compensation" not in t, max_details=120, lastmod_days=1000, boards=BOARDS):
    spec = SourceSpec(id="getro", adapter="getro",
                      params={"boards": boards, "max_details": max_details, "lastmod_days": lastmod_days})
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=60, respect_robots=True) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=wants, known=known or {})
        return list(get_adapter(spec.adapter)(ctx)), ctx, http


def test_parse_dedupes_overlap_across_boards():
    posts, ctx, http = _run()
    # clio, amenitiz, qlub, shapr3d (listed on both boards, fetched once), 9amhealth
    assert sorted(p.source_job_id for p in posts) == ["70526033", "78759824", "96377579", "97433732", "97485351"]
    assert len({p.posting_key for p in posts}) == len(posts)
    assert ctx.skipped == 2  # the title-gated compensation row + the stale (2023) sitemap row
    assert http.stats.requests == 4 + 5  # 2 robots + 2 sitemaps + 5 detail pages


def test_posting_fields():
    posts, _, _ = _run()
    p = {x.source_job_id: x for x in posts}["97433732"]
    assert p.title == "Senior Data Engineer" and p.company == "Amenitiz"
    assert p.url == "https://jobs.pointnine.com/companies/amenitiz-2/jobs/97433732-senior-data-engineer"
    assert p.apply_url.startswith("https://jobs.ashbyhq.com/amenitiz/")
    assert (p.ats_type, p.ats_slug) == ("ashby", "amenitiz")
    assert p.location_raw == "Barcelona, Spain" and p.countries == ["ES"] and p.workplace is None
    assert len(p.description) > 500 and "<p>" not in p.description
    assert p.posted_at is not None and p.employment_types == ["full_time"]
    remote = {x.source_job_id: x for x in posts}["96377579"]
    assert remote.workplace == "remote" and "CA" in remote.countries and remote.remote_scope
    assert remote.salary_min and remote.salary_currency == "CAD" and remote.salary_period == "year"
    assert remote.apply_url.startswith("https://clio.wd3.myworkdayjobs.com/")


def test_known_postings_are_not_fetched():
    known = {short_hash("getro", "97485351"): "x", short_hash("getro", "96377579"): "x"}
    posts, ctx, http = _run(known=known)
    assert {p.source_job_id for p in posts} == {"70526033", "78759824", "97433732"}
    assert sorted(ctx.seen_known) == sorted(known)
    assert http.stats.requests == 4 + 3


def test_detail_budget_and_stale_rows():
    posts, ctx, _ = _run(max_details=2)
    assert len(posts) == 2 and any("left for the next runs" in n for n in ctx.notes)
    posts, ctx, http = _run(lastmod_days=1)
    assert posts == [] and http.stats.requests == 4


def test_missing_sitemap_is_noted_not_fatal():
    posts, ctx, _ = _run(boards=["jobs.joinef.com", "jobs.pointnine.com"])
    assert len(posts) == 4 and any("jobs.joinef.com" in n for n in ctx.notes)
