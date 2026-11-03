"""SOLID.Jobs via sitemap_jsonld + solidjobs_ngstate enricher (offline replay).

Fixtures recorded live 2026-10-22; sitemap-offers.xml trimmed to 30 URLs (incl. two May-2026 offers that the
lastmod filter must skip), offer pages reduced to JSON-LD + the ng-state jobOfferDetails subset.
"""

import re
from datetime import datetime, timezone
from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "solidjobs"
GATE = re.compile(r"\b(?:data|database|bi|etl|analytics|ai|danych)\b")


def _params() -> dict:
    # cutoff between the stale (May) and fresh (Oct) fixture offers, whatever today's date is
    days = (datetime.now(timezone.utc) - datetime(2026, 7, 1, tzinfo=timezone.utc)).days
    return {
        "sitemaps": "https://solid.jobs/sitemap-offers.xml",
        "url_include": [r"/offer/\d+/"],
        "id_regex": r"/offer/(?P<id>\d+)/",
        "slug_regex": r"/offer/\d+/(?P<slug>[^/?#]+)",
        "lastmod_days": days,
        "max_details": 30,
        "enrich": "solidjobs_ngstate",
        "default_currency": "PLN",
        "skip_expired": False,
    }


def test_parse_offers():
    spec = SourceSpec(id="solidjobs", adapter="sitemap_jsonld", params=_params(), default_country="PL")
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=100,
                      respect_robots=True, browser=True) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=lambda t: bool(GATE.search(t)))
        posts = list(get_adapter(spec.adapter)(ctx))
        assert "404" not in http.stats.status_counts  # stale May offers skipped by lastmod
    assert len(posts) == 5
    assert all(p.title and p.company and p.url and p.description for p in posts)
    assert all(p.countries == ["PL"] and p.salary_min for p in posts)
    by_id = {p.source_job_id: p for p in posts}
    assert by_id["39337"].workplace == "hybrid"
    assert (by_id["39337"].salary_min, by_id["39337"].salary_max, by_id["39337"].salary_period) == (100.0, 135.0, "hour")
    assert "b2b" in by_id["39337"].tags
    assert by_id["39331"].workplace == "remote"
    assert by_id["39278"].workplace == "onsite"  # remotePossible: Brak
    ss = by_id["39234"]
    assert ss.company == "State Street Polska" and ss.salary_period == "year" and "uop" in ss.tags
