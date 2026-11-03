from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "thehub"
PARAMS = {"countries": ["EU", "REMOTE"], "queries": ["data"], "max_pages": 1, "max_details": 4}


def _strict_title(title):
    from fakes import fixture_engine

    # title-only check without the pre-gate allowance, so the recorded detail fixtures stay the same
    return fixture_engine().title_verdict(title).tier != "excluded"


def _run(**ctx_kw):
    spec = SourceSpec(id="thehub", adapter="thehub", params=PARAMS)
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=50) as http:
        return list(get_adapter(spec.adapter)(FetchContext(spec=spec, http=http, **ctx_kw)))


def test_parse():
    from jobradar.match.engine import MatchEngine

    posts = _run(wants_detail=_strict_title)
    assert len(posts) > 50 and all(p.title and p.url and p.source == "thehub" for p in posts)
    assert len({p.source_job_id for p in posts}) == len(posts)
    assert all(p.url.startswith("https://thehub.io/jobs/") for p in posts)
    # structured country names mapped to ISO-2; remote list rows flagged remote
    assert all(len(c) == 2 and c.isupper() for p in posts for c in p.countries)
    assert any(p.workplace == "remote" for p in posts)
    # detail endpoint merged for title-relevant rows: HTML description → text, publishedAt, ATS link
    detailed = [p for p in posts if p.description]
    assert len(detailed) == 4
    assert all(p.posted_at and "<" not in p.description for p in detailed)
    assert any(p.apply_url and p.apply_url.startswith("http") for p in detailed)


def test_known_postings_skip_detail():
    from jobradar.match.engine import MatchEngine
    from jobradar.models import short_hash

    first = _run(wants_detail=_strict_title)
    known = {short_hash("thehub", p.source_job_id): "x" for p in first if p.description}
    again = _run(wants_detail=_strict_title, known=known)
    assert len(known) == 4
    # known title-relevant postings are neither refetched nor re-yielded list-level (no version churn)
    assert not any(short_hash("thehub", p.source_job_id) in known for p in again)
    assert len(again) < len(first)
