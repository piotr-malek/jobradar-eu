"""Bulldogjob.pl via sitemap_jsonld (gzipped sitemap index) + bulldogjob_next enricher (offline replay).

Fixtures recorded live 2026-10-22; jobs.xml.gz trimmed to 30 URLs (re-gzipped), offer pages reduced to
their JSON-LD block + the ``__NEXT_DATA__`` job object.
"""

import re
from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "bulldogjob"

PARAMS = {
    "sitemap_index": "https://bulldogjob.pl/sitemap.xml.gz",
    "sitemap_include": r"jobs\.xml",
    "url_include": [r"/companies/jobs/\d+-"],
    "id_regex": r"/companies/jobs/(?P<id>\d+)-",
    "slug_regex": r"/companies/jobs/\d+-(?P<slug>[^/?#]+)",
    "max_details": 40,
    "enrich": "bulldogjob_next",
    "default_currency": "PLN",
    "skip_expired": False,
}
GATE = re.compile(r"\b(?:data|database|bi|etl|analytics|ai)\b")


def _run(**overrides):
    spec = SourceSpec(id="bulldogjob", adapter="sitemap_jsonld", params={**PARAMS, **overrides}, default_country="PL")
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=100,
                      respect_robots=True, browser=True) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=lambda t: bool(GATE.search(t)))
        posts = list(get_adapter(spec.adapter)(ctx))
        return posts, ctx, http.stats


def test_parse_offers():
    posts, ctx, stats = _run()
    assert "404" not in stats.status_counts
    assert len(posts) == 4
    assert all(p.title and p.company and p.url and p.description for p in posts)
    assert all(p.source_job_id.isdigit() for p in posts)
    assert all(p.workplace in ("remote", "hybrid", "onsite") for p in posts)
    by_id = {p.source_job_id: p for p in posts}

    roche = by_id["258902"]
    assert roche.company == "Roche Diagnostics"
    assert roche.workplace == "remote"
    assert "PL" in roche.countries and {"Warsaw", "Poznan"} <= set(roche.cities)
    assert (roche.salary_min, roche.salary_currency, roche.salary_period) == (7500.0, "PLN", "month")
    assert "uop" in roche.tags

    f5 = by_id["258345"]
    assert f5.workplace == "hybrid"  # workModes on-site + hybrid
    assert any("sitemap_urls=30" in n and "url_filter=28" in n for n in ctx.notes)


def test_expired_offers_are_dropped():
    posts, ctx, _ = _run(skip_expired=True)
    assert "256264" not in {p.source_job_id for p in posts}  # validThrough 2026-10-22
