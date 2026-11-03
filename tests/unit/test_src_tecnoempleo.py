"""Tecnoempleo (ES) — replayed search pages (remote + hybrid slice) and JSON-LD detail pages."""

import gzip
import json
from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from fakes import fixture_engine
from jobradar.models import short_hash
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.boards.tecnoempleo import parse_list
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "tecnoempleo"
PARAMS = {"queries": ["data engineer"], "modes": [1, 3], "max_pages": 1, "max_details": 3}


def _run(**kw):
    spec = SourceSpec(id="tecnoempleo", adapter="tecnoempleo", params=PARAMS, default_country="ES")
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=50) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=fixture_engine().wants_detail, **kw)
        return list(get_adapter(spec.adapter)(ctx)), ctx


def test_parse_posting():
    posts, ctx = _run()
    assert len(posts) == 3 and ctx.skipped > 0
    p = posts[0]
    assert p.title and p.company and p.countries == ["ES"] and p.cities
    assert p.workplace == "remote" and p.posted_at and len(p.description) > 300
    assert p.url.startswith("https://www.tecnoempleo.com/") and "/rf-" in p.url and p.apply_url is None
    assert len({x.source_job_id for x in posts}) == 3


def test_list_labels_work_mode():
    bodies = []
    for meta in sorted(FIX.glob("*.meta.json")):
        if json.loads(meta.read_text(encoding="utf-8"))["url"].endswith("/ofertas-trabajo/"):
            bodies.append(gzip.decompress(meta.with_name(meta.name.replace(".meta.json", ".body.gz")).read_bytes()).decode())
    remote, _ = parse_list(bodies[0])
    hybrid, total = parse_list(bodies[1])
    assert remote and {r["mode"] for r in remote} == {"remote"}
    assert total > 0 and "hybrid" in {r["mode"] for r in hybrid}


def test_known_postings_skip_detail():
    first, _ = _run()
    known = {short_hash("tecnoempleo", p.source_job_id): "x" for p in first}
    again, ctx = _run(known=known)
    assert all(p.source_job_id not in {f.source_job_id for f in first} for p in again)
    assert len(ctx.seen_known) == len(first)
