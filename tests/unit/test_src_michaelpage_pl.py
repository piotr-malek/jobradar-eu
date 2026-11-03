"""michaelpage_pl via sitemap_jsonld (keyword listing pages instead of a sitemap); offline replay of fixtures recorded 2026-10-24 (pages reduced to JSON-LD)."""

import re
from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "michaelpage_pl"
PARAMS = {
    "listing_urls": [
        "https://www.michaelpage.pl/jobs/data",
        "https://www.michaelpage.pl/jobs/data-engineer",
        "https://www.michaelpage.pl/jobs/data-analyst",
        "https://www.michaelpage.pl/jobs/analytics-engineer",
        "https://www.michaelpage.pl/jobs/bi",
        "https://www.michaelpage.pl/jobs/power-bi",
        "https://www.michaelpage.pl/jobs/snowflake"
    ],
    "listing_link_regex": "/job-detail/[^/]+/ref/jn-",
    "id_regex": "/ref/(?P<id>jn-\\d+-\\d+)",
    "slug_regex": "/job-detail/(?P<slug>[^/?#]+)/ref/",
    "slug_strip": [
        "-it-contracting$"
    ],
    "prefer": [
        "analytics-engineer|data-engineer|data-platform|dwh|etl|dbt|bigquery|snowflake|databricks|power-bi",
        "data|analytics|-bi-|business-intelligence"
    ],
    "max_details": 20,
    "default_currency": "PLN"
}
GATE = re.compile(r"\b(?:data|etl|bi|analytics|analyst|snowflake|fabric)\b")


def _run():
    spec = SourceSpec(id="michaelpage_pl", adapter="sitemap_jsonld", params=PARAMS, default_country='PL')
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=100,
                      respect_robots=True, browser=False) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=lambda t: bool(GATE.search(t)))
        return list(get_adapter(spec.adapter)(ctx)), ctx, http.stats


def test_michaelpage_pl_postings():
    posts, ctx, stats = _run()
    assert len(posts) >= 5
    assert all(p.title and p.company and p.url and p.description for p in posts)
    assert len({p.source_job_id for p in posts}) == len(posts)
    assert all("Michael Page" in p.company for p in posts)
    assert all(re.fullmatch(r"jn-\d+-\d+", p.source_job_id) for p in posts)
    assert any("listing" not in n and "sitemap_urls=" in n for n in ctx.notes)

