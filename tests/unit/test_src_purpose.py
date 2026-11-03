"""UN Careers + EU Careers adapters — replayed fixtures."""

from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.boards.purpose import parse_eu_detail, parse_eu_list
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures"


def _run(sid, **params):
    spec = SourceSpec(id=sid, adapter=sid, params=params)
    with PoliteClient(sid, transport=ReplayTransport(FIX / sid), sleep=False, max_requests=80) as http:
        ctx = FetchContext(spec=spec, http=http)
        posts = list(get_adapter(sid)(ctx))
    return posts, ctx


def test_un_careers():
    posts, ctx = _run("un_careers")
    assert len(posts) > 300 and ctx.skipped > 50  # internships dropped and counted
    assert len({p.source_job_id for p in posts}) == len(posts)
    assert all(p.description and p.url.startswith("https://careers.un.org/jobSearchDescription/") for p in posts)
    assert not any("category:INT" in p.tags for p in posts)
    p3 = next(p for p in posts if p.title.startswith("Information Systems Officer, P3") and "Brindisi" in p.location_raw)
    assert "grade:P-3" in p3.tags and "category:PD" in p3.tags and p3.countries == ["IT"]
    assert p3.company == "Department of Operational Support"
    g6 = next(p for p in posts if "grade:G-6" in p.tags)
    assert "category:GS" in g6.tags
    assert any("grade:NO-B" in p.tags and "category:NPO" in p.tags for p in posts)
    home = [p for p in posts if p.workplace == "remote"]
    assert home and all(p.location_raw.startswith("Home-based") for p in home)
    assert next(p for p in posts if "Valencia" in p.location_raw).countries == ["ES"]


def test_eu_careers():
    posts, ctx = _run("eu_careers")
    assert len(posts) >= 20
    assert len({p.source_job_id for p in posts}) == len(posts)
    assert all(p.url.startswith("https://eu-careers.europa.eu/jobs/") and p.description for p in posts)
    ems = next(p for p in posts if p.title.startswith("IT Officer - Software Engineering"))
    assert "grade:AD 6" in ems.tags and ems.employment_types == ["Temporary staff"]
    assert "European Medicines Agency" in ems.company and ems.countries == ["NL"]
    assert any("grade:FG IV" in p.tags for p in posts)
    assert any(t == "Information Technology" for p in posts for t in p.tags)


def test_eu_parsers():
    html = (
        '<div class="eujmp-vacancy-result"><ul class="eujmp-vacancy-result__top-meta"><li>European Commission</li>'
        '<li><time datetime="2026-10-26T11:00:00+00:00">26 October 2026</time></li></ul>'
        '<a href="/jobs/data-analyst_en" class="ecl-link eujmp-vacancy-result__title">Data Analyst</a>'
        '<ul class="eujmp-vacancy-result__bottom-meta"><li>Contract staff</li><li>FG IV</li>'
        '<li><span class="x"></span>Brussels, Belgium</li></ul></div></div></div>')
    rows = parse_eu_list(html)
    assert rows == [{"path": "/jobs/data-analyst_en", "title": "Data Analyst", "institution": "European Commission",
                     "deadline": "2026-10-26T11:00:00+00:00", "contract": "Contract staff", "grade": "FG IV",
                     "location": "Brussels, Belgium"}]
    assert parse_eu_detail("<x>")["fields"] == {}
