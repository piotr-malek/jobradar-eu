"""getdataengineeringjobs via sitemap_jsonld (filtered listing pages, no sitemap) + the ``gdej_facts`` enricher;
offline replay of fixtures recorded 2026-10-31 (page 1 of each listing URL, 6 detail pages)."""

from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.models import Posting
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.boards.getdataengineeringjobs import enrich_gdej, facts
from jobradar.sources.registry import get_adapter, load_specs

FIX = Path(__file__).parents[1] / "fixtures" / "getdataengineeringjobs"


def _run():
    from fakes import fixture_engine

    live = load_specs(all_sources=True)["getdataengineeringjobs"]  # the config's listing URLs are the recorded ones
    spec = SourceSpec(id=live.id, adapter=live.adapter, params={**live.params, "listing_pages": 1, "max_details": 6})
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=20,
                      respect_robots=True) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=fixture_engine().wants_detail)
        return {p.source_job_id: p for p in get_adapter(spec.adapter)(ctx)}, ctx


def test_listings_and_detail_pages():
    posts, ctx = _run()
    assert list(posts) == ["15083", "15082", "15071", "15052", "15035", "15033"]  # newest ids first
    assert all(p.title and p.company and p.description and p.posted_at for p in posts.values())
    assert all(p.url.startswith(f"https://getdataengineeringjobs.com/jobs/{i}-") for i, p in posts.items())
    assert "sitemap_urls=35 (docs=2)" in ctx.notes[-1] and "no_jsonld=0" in ctx.notes[-1]


def test_facts_set_work_mode_and_remote_eligibility():
    from fakes import fixture_engine

    posts, _ = _run()
    muttdata = posts["15083"]  # JSON-LD: Buenos Aires + TELECOMMUTE; page: Remote, eligibility Worldwide
    assert (muttdata.workplace, muttdata.remote_scope, muttdata.location_raw, muttdata.countries) == (
        "remote", "Worldwide", "Remote (Worldwide)", [])
    assert fixture_engine().match(muttdata).section == "remote"  # was "remote outside EU (AR)"
    tiger = posts["15052"]  # "On-site, Remote" with no restriction stated: the office stays
    assert (tiger.workplace, tiger.remote_scope, tiger.countries) == ("remote", None, ["US"])
    assert posts["15035"].workplace == "onsite"  # Nettbil, Oslo
    assert posts["15033"].workplace is None  # Rabot Energy: not specified
    assert posts["15033"].raw["facts"]["Location"] == "Hamburg, Germany; Berlin, Germany"


def test_facts_country_list():
    page = ('<dl class="facts"><div><dt>Work arrangement</dt><dd>Hybrid, Remote</dd></div>'
            "<div><dt>Remote eligibility</dt><dd>Croatia, Ireland, Poland</dd></div>"
            '<div><dt>Visa sponsorship</dt><dd><span class="text-muted">Not specified</span></dd></div></dl>')
    assert facts(page) == {"Work arrangement": "Hybrid, Remote", "Remote eligibility": "Croatia, Ireland, Poland"}
    post = Posting(source="getdataengineeringjobs", source_job_id="1", title="Data Engineer", company="X",
                   url="https://getdataengineeringjobs.com/jobs/1-x/", location_raw="Austin, US", countries=["US"])
    enrich_gdej(post, page, None)
    assert (post.workplace, post.location_raw, post.countries) == (
        "remote", "Remote (Croatia, Ireland, Poland)", ["HR", "IE", "PL"])
