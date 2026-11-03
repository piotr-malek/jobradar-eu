from pathlib import Path

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.normalize.lang import language_requirements
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures" / "remoterocketship"


def _run():
    spec = SourceSpec(id="remoterocketship", adapter="remoterocketship", params={})
    with PoliteClient(spec.id, transport=ReplayTransport(FIX), sleep=False, max_requests=40) as http:
        ctx = FetchContext(spec=spec, http=http)
        return list(get_adapter(spec.adapter)(ctx)), ctx


def test_parse():
    posts, ctx = _run()
    assert len(posts) > 60
    assert len({p.source_job_id for p in posts}) == len(posts)  # deduped across country/role pages
    assert all(p.title and p.url.startswith("http") and p.workplace for p in posts)
    assert all(p.description and p.remote_scope for p in posts)
    assert any("no paging" in n for n in ctx.notes)


def test_required_languages_reach_the_matcher():
    posts, _ = _run()
    needs = [p for p in posts if "lang:de" in p.tags or "lang:fr" in p.tags]
    assert needs
    for p in needs:
        req = language_requirements(p.description)
        assert "required" in req.values()
    # English/Polish-only rows add no language sentence
    plain = [p for p in posts if p.tags and set(t for t in p.tags if t.startswith("lang:")) <= {"lang:en", "lang:pl"}]
    assert plain and not any(language_requirements(p.description) for p in plain[:20] if "German" not in p.description)


def test_unknown_role_page_is_ignored():
    from jobradar.sources.boards.remoterocketship import parse_page

    html = '<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"initialFilters":' \
           '{"jobTitleFilters":[]},"initialJobOpenings":[{"id":1}],"initialTotalJobCount":10969}}}</script>'
    assert parse_page(html) == ([], 0)
