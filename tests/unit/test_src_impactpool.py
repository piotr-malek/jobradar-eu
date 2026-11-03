import json
from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.boards.impactpool import parse_detail, parse_list
from jobradar.sources.boards.un_grades import grade_tag, is_non_job
from jobradar.sources.registry import get_adapter
from jobradar.models import short_hash

FIX = Path(__file__).parents[1] / "fixtures" / "impactpool"
PARAMS = {"max_pages": 3, "per_page": 100, "max_details": 44}


def _recorded_ids() -> set[str]:
    ids = set()
    for m in FIX.glob("*.meta.json"):
        url = json.loads(m.read_text(encoding="utf-8"))["url"]
        if "/jobs/" in url:
            ids.add(url.rsplit("/", 1)[-1])
    return ids


def _run(**ctx_kw):
    spec = SourceSpec(id="impactpool", adapter="impactpool", params=PARAMS)
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=80) as http:
        ctx = FetchContext(spec=spec, http=http, **ctx_kw)
        return list(get_adapter(spec.adapter)(ctx)), ctx


def _gate():
    """wants_detail limited to titles whose detail page was recorded (the recorder used the matcher's gate)."""
    from jobradar.sources.boards.impactpool import parse_list as pl

    ids = _recorded_ids()
    titles = set()
    for m in FIX.glob("*.meta.json"):
        meta = json.loads(m.read_text(encoding="utf-8"))
        if "/search?" in meta["url"]:
            import gzip

            body = gzip.decompress(m.with_name(m.name.replace(".meta.json", ".body.gz")).read_bytes()).decode()
            titles |= {r["title"] for r in pl(body) if r["id"] in ids}
    return lambda t: t in titles


def test_grade_tags():
    assert grade_tag("P-3, International Professional - Internationally recruited position - Mid level") == "grade:P-3"
    assert grade_tag("GS-6, General Service - No need for Higher Education - Locally recruited") == "grade:GS-6"
    assert grade_tag("NO-B, National Professional Officer - Locally recruited position - Junior level") == "grade:NO-B"
    assert grade_tag("IP3, International Professional 3 - Internationally recruited position") == "grade:IP-3"
    assert grade_tag("D-1, Director - Internationally recruited position") == "grade:D-1"
    assert grade_tag("Consultant - Contractors Agreement - Consultancy") == "grade:Consultant"
    assert grade_tag("Mid - Mid level") is None
    assert is_non_job("Internship - Internship") and not is_non_job("Information Management Officer")


def test_listing_and_details():
    posts, ctx = _run(wants_detail=_gate())
    assert len(posts) > 100 and len({p.source_job_id for p in posts}) == len(posts)
    assert all(p.title and p.company and p.url.startswith("https://www.impactpool.org/jobs/") for p in posts)
    assert ctx.skipped >= 0
    grades = [t for p in posts for t in p.tags if t.startswith("grade:")]
    assert any(g.startswith("grade:P-") for g in grades) and "grade:Consultant" in grades
    assert not any("internship" in p.raw["level"].lower() for p in posts)
    detailed = [p for p in posts if p.description]
    assert len(detailed) >= 20 and all("<" not in p.description for p in detailed)
    assert any(p.workplace == "remote" for p in posts)


def test_known_postings_not_refetched():
    first, _ = _run(wants_detail=_gate())
    known = {short_hash("impactpool", first[0].source_job_id): "x"}
    again, ctx = _run(wants_detail=_gate(), known=known)
    assert len(again) == len(first) - 1 and ctx.seen_known == list(known)


def test_parsers():
    page = (FIX / "dummy").exists()  # keep parsers import-checked even without network
    assert not page
    assert parse_list("<html></html>") == []
    d = parse_detail("<html><main><div id='job-description'><h1>T</h1></div></main></html>")
    assert d["title"] == "T" and d["description"] == ""
