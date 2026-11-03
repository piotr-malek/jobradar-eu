"""Boards added 2026-10-26 on existing adapters (sitemap_jsonld, jobsch); params come from config/sources.yaml.

Offline replay of fixtures recorded 2026-10-26 with max_details 4: each sitemap is trimmed to the 4 fetched job URLs
plus 15 URLs that fail the title gate, so the lastmod cutoff is dropped here (it depends on today's date).
"""

import re
from pathlib import Path

import pytest

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext
from jobradar.sources.registry import get_adapter, load_specs

FIX = Path(__file__).parents[1] / "fixtures"
GATE = re.compile(r"\b(?:data|etl|bi|analytics|analyst|ai|automation|integration)\b", re.I)
SITEMAP_BOARDS = {"barcelonajobs": "ES", "magnet_me": "NL", "engineeringjobs_be": "BE", "engineeringjobs_nl": "NL",
                  "ictjobs_ch": "CH", "jobsinmalta": "MT", "cyprusjobs": "CY"}


def _run(sid: str, **params):
    spec = load_specs(all_sources=True)[sid]
    spec.params = {k: v for k, v in {**spec.params, **params}.items() if v is not None}
    with PoliteClient(sid, transport=ReplayTransport(FIX / sid), sleep=False, max_requests=60, respect_robots=True,
                      browser=bool(spec.politeness.get("browser"))) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=lambda t: bool(GATE.search(t)))
        return list(get_adapter(spec.adapter)(ctx))


@pytest.mark.parametrize("sid,country", sorted(SITEMAP_BOARDS.items()))
def test_sitemap_board(sid, country):
    posts = _run(sid, max_details=4, lastmod_days=None, skip_expired=False)    # recorded postings age out
    assert len(posts) == 4
    assert len({p.source_job_id for p in posts}) == 4
    assert all(p.title and p.company and p.url and len(p.description) > 200 for p in posts)
    assert all(country in p.countries for p in posts if p.countries)
    assert sum(bool(p.countries) for p in posts) >= 3


def test_jobup_uses_its_own_site_and_paths():
    posts = _run("jobup_ch", queries=["data engineer"], max_pages=1, max_details=3)
    assert len(posts) == 20
    assert all(p.url.startswith("https://www.jobup.ch/en/jobs/detail/") for p in posts)
    detailed = [p for p in posts if p.description]
    assert len(detailed) == 3 and all(len(p.description) > 300 for p in detailed)
    assert all(p.countries == ["CH"] for p in posts if p.countries)
