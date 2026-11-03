"""NAV pam-stilling-feed adapter — replayed fixtures (public token replaced by a dummy)."""

from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "nav_no"
PARAMS = {"lookback_hours": 26, "title_regex": r"data|analyt|\bbi\b|etl|dbt"}


def test_parse():
    spec = SourceSpec(id="nav_no", adapter="nav_feed", params=PARAMS, default_country="NO")
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=50) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=lambda t: False)
        posts = list(get_adapter(spec.adapter)(ctx))
    ids = {p.source_job_id for p in posts}
    assert {"5527c23e-12cb-46db-a143-a61308f0ec76", "0262fb83-4254-4967-a3e7-a7695d150636"} <= ids
    assert all(p.title and p.url.startswith("https://arbeidsplassen.nav.no/") for p in posts)
    assert all(p.countries == ["NO"] for p in posts)
    assert all(p.description and "<" not in p.description[:300] for p in posts)
    assert all("contact" not in str(p.raw).lower() for p in posts)
    # token + 4 feed pages (followed next_url to the end) + details
    urls = [u for u in ReplayTransport(FIX)._by_url]
    assert any(u.endswith("/api/publicToken") for u in urls)


def test_title_gate_skips_everything_without_regex():
    spec = SourceSpec(id="nav_no", adapter="nav_feed", params={"lookback_hours": 26}, default_country="NO")
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=50) as http:
        posts = list(get_adapter(spec.adapter)(FetchContext(spec=spec, http=http, wants_detail=lambda t: False)))
        assert posts == [] and http.stats.requests == 5  # token + 4 pages, no details


def test_private_token_skips_public_token():
    spec = SourceSpec(id="nav_no", adapter="nav_feed", params={"lookback_hours": 26, "use_public_token": False})
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=50) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=lambda t: False, secrets={"NAV_FEED_TOKEN": "x"})
        list(get_adapter(spec.adapter)(ctx))
        assert http.stats.requests == 4
