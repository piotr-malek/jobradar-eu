import json
from pathlib import Path

import httpx

from jobradar.http.client import PoliteClient, ReplayTransport
from fakes import fixture_engine
from jobradar.models import short_hash
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.boards.unicorns_lt import parse_cards
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "unicorns_lt"


class _Replay(ReplayTransport):
    """ReplayTransport that also serves the Location header recorded (by hand) in the meta of 302 answers."""

    def __init__(self, d: Path):
        super().__init__(d)
        self._loc = {}
        for m in d.glob("*.meta.json"):
            meta = json.loads(m.read_text(encoding="utf-8"))
            if meta.get("location"):
                self._loc[meta["url"]] = meta["location"]

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        resp = super().handle_request(request)
        loc = self._loc.get(str(request.url))
        return httpx.Response(resp.status_code, headers={"location": loc}, request=request) if loc else resp


PARAMS = {"categories": ["Data & AI", "Other"], "max_pages": 2, "max_details": 3}


def _run(**kw):
    spec = SourceSpec(id="unicorns_lt", adapter="unicorns_lt", params=PARAMS, default_country="LT")
    with PoliteClient(spec.id, transport=_Replay(FIX), sleep=False, max_requests=100) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=fixture_engine().wants_detail, **kw)
        return list(get_adapter("unicorns_lt")(ctx)), ctx


def test_parse_cards_and_apply_links():
    posts, ctx = _run()
    assert len(posts) == 3 and ctx.skipped > 10
    p = next(x for x in posts if x.company == "Vinted")
    assert p.title.startswith("Analytics Engineer") and p.countries == ["LT"] and p.posted_at
    assert p.url == f"https://unicorns.lt/j/{p.source_job_id}"
    assert p.apply_url and p.apply_url.startswith("https://careers.vinted.com/")  # 302 target, not followed
    assert all(x.apply_url and "unicorns.lt" not in x.apply_url for x in posts)


def test_city_clones_are_one_posting():
    posts, _ = _run()
    keys = [(p.company.lower(), p.title.lower()) for p in posts]
    assert len(keys) == len(set(keys))
    html = ('<div class="card listing"><h3>A</h3><div class="company">Co, Vilnius</div>'
            '<a href="/j/1"></a></div>')
    assert parse_cards(html)[0]["city"] == "Vilnius" and parse_cards(html)[0]["company"] == "Co"


def test_known_postings_skip_redirect_lookup():
    first, _ = _run()
    known = {short_hash("unicorns_lt", p.source_job_id): "x" for p in first}
    again, ctx = _run(known=known)
    assert len(ctx.seen_known) == 3 and not {p.source_job_id for p in again} & {p.source_job_id for p in first}
