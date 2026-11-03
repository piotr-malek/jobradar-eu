"""The Muse public API adapter — replayed fixtures."""

import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.boards.public_boards import _country_from_place
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "themuse"


def _recorded_locations() -> list[str]:
    """The location list the fixtures were recorded with (the default list has changed since)."""
    url = json.loads((FIX / "001.meta.json").read_text())["url"]
    return [v for k, v in parse_qsl(urlsplit(url).query) if k == "location"]


def test_parse():
    params = {"max_pages": 4, "locations": _recorded_locations()}
    spec = SourceSpec(id="themuse", adapter="themuse", params=params)
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=50) as http:
        posts = list(get_adapter(spec.adapter)(FetchContext(spec=spec, http=http)))
        assert http.stats.requests == 4
    assert len(posts) >= 20 and all(p.title and p.url.startswith("https://www.themuse.com/") for p in posts)
    assert len({p.source_job_id for p in posts}) == len(posts)
    assert all(p.description and "<" not in p.description[:300] for p in posts)
    assert all(len(c) == 2 for p in posts for c in p.countries)
    for p in posts:
        if p.workplace == "remote":
            assert p.location_raw == "Flexible / Remote"


def test_place_to_country():
    assert _country_from_place("Berlin, Germany") == "DE"
    assert _country_from_place("Austin, TX") == "US"
    assert _country_from_place("London, United Kingdom") == "GB"
    assert _country_from_place("Flexible / Remote") is None
