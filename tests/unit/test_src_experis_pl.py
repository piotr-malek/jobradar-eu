"""experis_pl via sitemap_jsonld (sitemap, EN copies only, bare-@id hiringOrganization); offline replay of fixtures recorded 2026-10-24 (pages reduced to JSON-LD)."""

import re
from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "experis_pl"
PARAMS = {
    "sitemaps": "https://www.experis.pl/sitemap.xml",
    "url_include": [
        "/en/jobb/\\d+/"
    ],
    "id_regex": "/jobb/(?P<id>\\d+)/",
    "slug_regex": "/jobb/\\d+/(?P<slug>[^/?#]+)",
    "default_company": "Experis",
    "prefer": [
        "analytics-engineer|data-engineer|data-platform|dwh|etl|dbt|bigquery|snowflake|databricks|power-bi",
        "data|analytics|-bi-|business-intelligence"
    ],
    "max_details": 30,
    "default_currency": "PLN"
}
GATE = re.compile(r"\b(?:data|etl|bi|analytics|analyst|snowflake|fabric)\b")


def _run():
    spec = SourceSpec(id="experis_pl", adapter="sitemap_jsonld", params=PARAMS, default_country='PL')
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=100,
                      respect_robots=True, browser=False) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=lambda t: bool(GATE.search(t)))
        return list(get_adapter(spec.adapter)(ctx)), ctx, http.stats


def test_experis_pl_postings():
    posts, ctx, stats = _run()
    assert len(posts) >= 5
    assert all(p.title and p.company and p.url and p.description for p in posts)
    assert len({p.source_job_id for p in posts}) == len(posts)
    assert all(p.company == "Experis" and "/en/jobb/" in p.url for p in posts)  # default_company; EN copy only
    assert all(p.posted_at is not None for p in posts)  # '20250701T084614' compact dates parsed
    assert all(p.countries == ["PL"] for p in posts)

