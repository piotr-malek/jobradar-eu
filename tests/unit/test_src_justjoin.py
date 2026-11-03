"""JustJoin.it via the generic sitemap_jsonld adapter + justjoin_rsc enricher (offline replay).

Fixtures recorded live 2026-10-22 with PoliteClient(record_dir=...), then trimmed: sitemap cut to 30
URLs (one synthetic other-city copy of the Hiberus offer added), offer pages reduced to their JSON-LD
block + the offer row of the RSC payload.
"""

import re
from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.models import short_hash
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "justjoin"

PARAMS = {
    "sitemap_index": "https://justjoin.it/sitemaps/active-jobs.xml",
    "url_include": ["/job-offer/"],
    "slug_strip": ["-[a-z]+(?:-[0-9a-f]{8})?$"],
    "group_strip": "-[a-z]+-[a-z]+(?:-[0-9a-f]{8})?$",
    "prefer": ["-data(?:-[0-9a-f]{8})?$"],
    "max_details": 80,
    "enrich": "justjoin_rsc",
    "default_currency": "PLN",
    # fixtures age: no lastmod/expiry filtering in tests
    "skip_expired": False,
}
GATE = re.compile(r"\b(?:data|database|bi|etl|analytics|ai)\b")


def _run(known: dict[str, str] | None = None, **overrides):
    spec = SourceSpec(id="justjoin", adapter="sitemap_jsonld", params={**PARAMS, **overrides}, default_country="PL")
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=100,
                      respect_robots=True, browser=True) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=lambda t: bool(GATE.search(t)), known=known or {})
        posts = list(get_adapter(spec.adapter)(ctx))
        return posts, ctx, http.stats


def test_parse_offers():
    posts, ctx, stats = _run()
    assert len(posts) == 5
    assert all(p.title and p.company and p.url and p.source == "justjoin" for p in posts)
    assert all(p.description and len(p.description) > 200 for p in posts)
    assert all(p.workplace in ("remote", "hybrid", "onsite") for p in posts)
    assert all(p.posted_at for p in posts)
    assert "404" not in stats.status_counts  # other-city copy was not fetched (grouped / covered)
    by_id = {p.source_job_id: p for p in posts}

    hib = by_id["hiberus-poland-data-platform-developer-wroclaw-data"]
    assert hib.workplace == "remote"
    assert hib.company == "Hiberus Poland"
    assert {"Wrocław", "Warszawa", "Kraków"} <= set(hib.cities)
    assert (hib.salary_min, hib.salary_max, hib.salary_currency, hib.salary_period) == (16800.0, 27300.0, "PLN", "month")
    assert hib.countries == ["PL"]
    assert "\n" in hib.description  # paragraphs kept (JSON-LD description squashes them)

    com = by_id["comarch-senior-data-engineer-krakow-data"]
    assert com.workplace == "hybrid" and com.cities == ["Kraków"] and com.countries == ["PL"]

    ema = next(p for p in posts if p.company == "emagine Polska")
    assert ema.workplace == "onsite" and ema.countries == ["DK"]
    assert any("sitemap_urls=30" in n and "parsed=5" in n for n in ctx.notes)


def test_known_postings_are_not_refetched():
    key = short_hash("justjoin", "comarch-senior-data-engineer-krakow-data")
    posts, ctx, stats = _run(known={key: "v1"})
    assert len(posts) == 4
    assert all(p.source_job_id != "comarch-senior-data-engineer-krakow-data" for p in posts)


def test_max_details_and_title_gate():
    posts, _, stats = _run(max_details=2)
    assert len(posts) == 2
    # robots + index + part0 + 2 details
    assert stats.requests == 5
