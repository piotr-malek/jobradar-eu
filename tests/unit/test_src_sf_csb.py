from pathlib import Path

import pytest

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures"
CASES = {
    "itu": ("jobs.itu.int", 8, {"grade:Consultant", "grade:P-4"}),
    "unesco": ("careers.unesco.org", 8, {"grade:Consultant"}),
    "ilo": ("jobs.ilo.org", 6, {"grade:NO-A", "grade:P-3"}),
}


def _run(sid, **ctx_kw):
    host, n, _ = CASES[sid]
    spec = SourceSpec(id=sid, adapter="sf_csb", params={"host": host, "org": sid.upper(), "max_details": n, "title_gate": False})
    with PoliteClient(sid, transport=ReplayTransport(FIX / sid), sleep=False, max_requests=40) as http:
        ctx = FetchContext(spec=spec, http=http, **ctx_kw)
        return list(get_adapter("sf_csb")(ctx)), ctx


@pytest.mark.parametrize("sid", list(CASES))
def test_parse(sid):
    posts, ctx = _run(sid)
    host, n, grades = CASES[sid]
    assert len(posts) >= n - 2 and all(p.title and p.company == sid.upper() for p in posts)
    assert all(p.url.startswith(f"https://{host}/job/") and p.description and "<" not in p.description for p in posts)
    seen = {t for p in posts for t in p.tags if t.startswith("grade:")}
    assert seen & grades, seen
    assert sum(bool(p.posted_at) for p in posts) >= len(posts) // 2
    assert not any("internship" in p.title.lower() for p in posts)


def test_itu_remote_and_internships_skipped():
    posts, ctx = _run("itu")
    assert any(p.workplace == "remote" and "Home Based" in p.location_raw for p in posts)
    assert ctx.skipped >= 9  # unpaid internships dropped from the sitemap slugs


def test_title_gate_and_known():
    from jobradar.models import short_hash

    posts, _ = _run("ilo")
    known = {short_hash("ilo", posts[0].source_job_id): "x"}
    again, ctx = _run("ilo", known=known)
    assert len(again) == len(posts) - 1 or len(again) == len(posts)
    assert ctx.seen_known == list(known)
