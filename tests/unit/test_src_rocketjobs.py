"""RocketJobs.pl (JustJoin's sister board for non-IT roles) — config-only reuse of sitemap_jsonld + justjoin_rsc.

Fixtures recorded live 2026-10-22, trimmed (30 sitemap URLs, 4 offer pages reduced to JSON-LD + RSC offer row).
"""

import re
from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "rocketjobs"

PARAMS = {
    "sitemap_index": "https://rocketjobs.pl/sitemaps/active-jobs.xml",
    "url_include": ["/oferta-pracy/"],
    "slug_strip": ["-[0-9a-f]{8}$", "-bi-data$"],
    "group_strip": "-[0-9a-f]{8}$",
    "prefer": ["-bi-data(?:-[0-9a-f]{8})?$"],
    "max_details": 30,
    "enrich": "justjoin_rsc",
    "default_currency": "PLN",
    "skip_expired": False,
}
GATE = re.compile(r"\b(?:data|database|bi|etl|analytics|ai|danych)\b")


def test_parse_offers():
    spec = SourceSpec(id="rocketjobs", adapter="sitemap_jsonld", params=PARAMS, default_country="PL")
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=100,
                      respect_robots=True, browser=True) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=lambda t: bool(GATE.search(t)))
        posts = list(get_adapter(spec.adapter)(ctx))
        assert "404" not in http.stats.status_counts
    assert len(posts) == 4
    assert all(p.title and p.company and p.url and p.description for p in posts)
    assert all(p.workplace in ("remote", "hybrid", "onsite") for p in posts)
    assert all(p.countries == ["PL"] for p in posts)
    by_company = {p.company: p for p in posts}
    cyc = by_company["Cyclad"]
    assert cyc.title == "Data Modeler" and cyc.workplace == "hybrid"
    assert (cyc.salary_min, cyc.salary_max, cyc.salary_currency, cyc.salary_period) == (110.0, 130.0, "PLN", "hour")
    assert "b2b" in cyc.tags
    remote = [p for p in posts if p.workplace == "remote"]
    assert remote and len(remote[0].cities) > 1
    assert all(", pl" not in p.location_raw for p in posts)
