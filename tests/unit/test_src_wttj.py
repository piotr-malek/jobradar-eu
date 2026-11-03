from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.boards.wttj import build_filters, read_env
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "wttj"


def _run():
    spec = SourceSpec(id="wttj", adapter="wttj", params={"queries": ["data engineer", "analytics engineer"], "max_pages": 1})
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=50) as http:
        return list(get_adapter(spec.adapter)(FetchContext(spec=spec, http=http)))


def test_parse():
    posts = _run()
    assert len(posts) > 100 and all(p.title and p.url and p.company for p in posts)
    assert len({p.source_job_id for p in posts}) == len(posts)
    assert all(p.url.startswith("https://www.welcometothejungle.com/en/companies/") for p in posts)
    assert all(p.description and p.posted_at for p in posts)
    assert {p.workplace for p in posts} <= {"remote", "hybrid"}  # filtered to remote fulltime/partial
    assert all(len(c) == 2 and c.isupper() and c not in ("US", "CA") for p in posts for c in p.countries)
    with_salary = [p for p in posts if p.salary_min]
    assert with_salary and all(p.salary_period in ("year", "month", "day", "hour", None) for p in with_salary)
    de = next(p for p in posts if p.title == "Senior Data Engineer" and p.company == "Implicity")
    assert de.workplace == "hybrid" and de.countries == ["FR"] and de.cities == ["Paris"]
    assert "Key missions:" in de.description


def test_env_and_filters():
    env = read_env('window.env = {"PUBLIC_ALGOLIA_APPLICATION_ID":"APP","PUBLIC_ALGOLIA_API_KEY_CLIENT":"k"};')
    assert env["PUBLIC_ALGOLIA_APPLICATION_ID"] == "APP"
    f = build_filters({"languages": ["en"], "exclude_countries": ["US"], "exclude_contracts": []})
    assert f == "(language:en) AND (remote:fulltime OR remote:partial) AND NOT offices.country_code:US"
