"""remoteleaf via sitemap_jsonld (sitemap, JSON-LD with applicantLocationRequirements); offline replay of fixtures recorded 2026-10-24 (pages reduced to JSON-LD)."""

import re
from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "remoteleaf"
PARAMS = {
    "sitemaps": "https://remoteleaf.com/sitemap-jobs-europe.xml",
    "url_include": [
        "/company/[^/]+/[^/]+/$"
    ],
    "id_regex": "/company/(?P<id>[^/]+/[^/]+)/$",
    "slug_regex": "/company/[^/]+/(?P<slug>[^/?#]+)/$",
    "prefer": [
        "analytics-engineer|data-engineer|data-platform|dwh|etl|dbt|bigquery|snowflake|databricks|power-bi",
        "data|analytics|-bi-|business-intelligence"
    ],
    "lastmod_days": 30,
    "max_details": 40
}
GATE = re.compile(r"\b(?:data|etl|bi|analytics|analyst|snowflake|fabric)\b")


def _run():
    spec = SourceSpec(id="remoteleaf", adapter="sitemap_jsonld", params=PARAMS, default_country=None)
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=100,
                      respect_robots=True, browser=False) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=lambda t: bool(GATE.search(t)))
        return list(get_adapter(spec.adapter)(ctx)), ctx, http.stats


def test_remoteleaf_postings():
    posts, ctx, stats = _run()
    assert len(posts) >= 5
    assert all(p.title and p.company and p.url and p.description for p in posts)
    assert len({p.source_job_id for p in posts}) == len(posts)
    assert all(p.workplace == "remote" for p in posts)
    # applicantLocationRequirements reach the posting (countries + remote_scope)
    assert any(p.remote_scope and p.countries for p in posts)
    assert all(p.source_job_id.count("/") == 1 for p in posts)

