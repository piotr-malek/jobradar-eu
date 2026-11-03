"""Adding a job by hand: the posting is read without an LLM (JSON-LD, else og:title + page text, ATS link on the page)."""

from __future__ import annotations

import json

import httpx

from jobradar.apply.posting import read_posting
from jobradar.apply.prep import PrepHttp

LD = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Analytics Engineer",
      "hiringOrganization": {"@type": "Organization", "name": "Acme"},
      "jobLocation": {"@type": "Place", "address": {"addressLocality": "Wrocław", "addressCountry": "PL"}},
      "baseSalary": {"currency": "PLN", "value": {"minValue": 20000, "maxValue": 26000, "unitText": "MONTH"}},
      "description": "<p>Build dbt models.</p><ul><li>BigQuery</li></ul>"}
FILLER = "<p>" + "We build data products for shops across Europe. " * 10 + "</p>"
PAGES = {
    "https://board.example/jobs/1": f'<html><script type="application/ld+json">{json.dumps(LD)}</script></html>',
    "https://careers.example/data": ('<html><head><meta property="og:title" content="Data Engineer">'
                                     '<meta property="og:site_name" content="Acme Analytics"></head><body>'
                                     '<nav>Home Jobs</nav><main><h1>Data Engineer</h1>' + FILLER +
                                     '<a href="https://jobs.lever.co/acme/0b5c3c3e-1111-4a5b-9c3d-123456789abc">Apply'
                                     '</a></main></body></html>'),
    "https://blocked.example/robots.txt": "User-agent: *\nDisallow: /",
}


def http() -> PrepHttp:
    def handle(req: httpx.Request) -> httpx.Response:
        body = PAGES.get(str(req.url))
        if body is None:
            return httpx.Response(404, text="nope")
        ctype = "text/plain" if str(req.url).endswith("robots.txt") else "text/html"
        return httpx.Response(200, text=body, headers={"content-type": ctype})

    return PrepHttp(transport=httpx.MockTransport(handle), sleep=False)


def test_jsonld_gives_role_company_location_salary_and_text():
    with http() as h:
        got = read_posting("https://board.example/jobs/1", h)
    assert (got["title"], got["company"]) == ("Analytics Engineer", "Acme")
    assert "Wrocław" in got["location"] and got["salary"] == "PLN 20k–26k/mo"
    assert "Build dbt models." in got["description"] and got["route"].vendor == "unknown" and not got["note"]


def test_careers_page_without_jsonld_uses_og_title_main_text_and_ats_link():
    with http() as h:
        got = read_posting("https://careers.example/data", h)
    assert got["title"] == "Data Engineer" and got["company"] == "Acme Analytics"     # og:site_name before the slug
    assert got["route"].vendor == "lever" and got["route"].job.startswith("0b5c3c3e")
    assert "data products" in got["description"] and "Home Jobs" not in got["description"]


def test_robots_disallow_leaves_fields_for_the_user():
    with http() as h:
        got = read_posting("https://blocked.example/job/9", h)
    assert not got["title"] and not got["description"] and "automated" in got["note"]


def test_company_from_slug_skips_short_slugs():
    with http() as h:
        assert read_posting("https://jobs.lever.co/acme-labs/0b5c3c3e-1111-4a5b-9c3d-123456789abc", h)["company"] \
            == "Acme Labs"
        assert read_posting("https://apply.workable.com/j/91E8CC5D16", h)["company"] == ""
