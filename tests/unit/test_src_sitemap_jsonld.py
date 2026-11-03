"""Generic sitemap_jsonld adapter: sitemap parsing, JSON-LD → Posting mapping, robots handling (no network)."""

import gzip
import json

import httpx

from jobradar.http.client import PoliteClient
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.generic.sitemap_jsonld import (
    contract_tags,
    country_iso2,
    decode_body,
    extract_jobposting,
    jobposting_to_posting,
    parse_sitemap,
)
from jobradar.sources.registry import get_adapter

INDEX = """<?xml version="1.0" encoding="UTF-8"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://ex.test/jobs.xml.gz</loc><lastmod>2026-10-22</lastmod></sitemap>
<sitemap><loc>https://ex.test/blog.xml</loc></sitemap></sitemapindex>"""
URLSET = """﻿<?xml version="1.0" encoding="utf-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://ex.test/job/1-senior-data-engineer</loc><lastmod>2026-10-21T10:00:00+00:00</lastmod></url>
<url><lastmod>2026-10-20</lastmod><loc>https://ex.test/job/2-java-developer?a=1&amp;b=2</loc></url>
<url><loc>https://ex.test/private/3-bi-developer</loc></url>
</urlset>"""


def _page(jp: dict) -> str:
    return ('<html><head><script type="application/ld+json">{"@context":"https://schema.org","@graph":['
            '{"@type":"Organization","name":"Board"},' + json.dumps(jp) + "]}</script></head><body></body></html>")


JP = {
    "@type": "JobPosting",
    "title": "Senior Data Engineer &amp; Analytics",
    "hiringOrganization": {"@type": "Organization", "name": "ACME"},
    "jobLocation": [{"@type": "Place", "address": {"addressLocality": "Wrocław", "addressCountry": "Polska"}},
                    {"@type": "Place", "address": {"addressLocality": "Berlin", "addressCountry": {"name": "Germany"}}}],
    "jobLocationType": "TELECOMMUTE",
    "applicantLocationRequirements": [{"@type": "Country", "name": f"C{i}"} for i in range(40)],
    "baseSalary": {"@type": "MonetaryAmount", "currency": "PLN",
                   "value": {"@type": "QuantitativeValue", "minValue": 20000, "maxValue": 28000, "unitText": "MONTH"}},
    "employmentType": ["FULL_TIME", "CONTRACTOR"],
    "datePosted": "2026-10-20",
    "validThrough": "2026-11-20T00:00:00Z",
    "description": "<p>Build dbt models.</p><p>Umowa o pracę lub B2B.</p>",
    "skills": "SQL, dbt",
}


def test_parse_sitemap_index_and_urlset():
    children, urls = parse_sitemap(INDEX)
    assert [c.loc for c in children] == ["https://ex.test/jobs.xml.gz", "https://ex.test/blog.xml"] and not urls
    children, urls = parse_sitemap(decode_body(gzip.compress(URLSET.encode())))
    assert not children and len(urls) == 3
    assert urls[1].loc == "https://ex.test/job/2-java-developer?a=1&b=2" and urls[1].lastmod.day == 20
    assert urls[2].lastmod is None


def test_parse_sitemap_cdata_and_whitespace():
    # Hays wraps both <loc> and <lastmod> in CDATA with padding; an unparsed lastmod would bypass lastmod_days
    text = """<urlset><url>
        <loc>
              <![CDATA[ https://www.hays.com/pl-PL/oferta-pracy-opis/data-engineer-wroc%C5%82aw_1201402 ]]>
        </loc>
        <lastmod>
              <![CDATA[ 2026-10-22T15:28+00:00 ]]>
        </lastmod></url></urlset>"""
    _, urls = parse_sitemap(text)
    assert urls[0].loc == "https://www.hays.com/pl-PL/oferta-pracy-opis/data-engineer-wroc%C5%82aw_1201402"
    assert urls[0].lastmod is not None and (urls[0].lastmod.day, urls[0].lastmod.hour) == (22, 15)


def test_country_iso2():
    assert country_iso2("PL") == "PL" and country_iso2("pl") == "PL"
    assert country_iso2("POL") == "PL" and country_iso2("Polska") == "PL" and country_iso2("Poland") == "PL"
    assert country_iso2({"@type": "Country", "name": "Germany"}) == "DE"
    assert country_iso2("Atlantis") is None and country_iso2(None) is None


def test_jobposting_mapping():
    jp = extract_jobposting(_page(JP))
    p = jobposting_to_posting(jp, source="x", source_job_id="1", url="https://ex.test/job/1")
    assert p.title == "Senior Data Engineer & Analytics" and p.company == "ACME"
    assert p.workplace == "remote" and p.remote_scope is None  # 40-country list ignored
    assert p.countries == ["PL", "DE"] and p.cities == ["Wrocław", "Berlin"]
    assert (p.salary_min, p.salary_max, p.salary_currency, p.salary_period) == (20000.0, 28000.0, "PLN", "month")
    assert p.description.startswith("Build dbt models.") and "\n" in p.description
    assert {"SQL", "dbt", "b2b", "uop"} <= set(p.tags)
    assert p.posted_at.year == 2026 and p.raw["validThrough"].startswith("2026-11-20")


def test_contract_tags():
    assert contract_tags("Forma: kontrakt B2B") == ["b2b"]
    assert contract_tags("umowa zlecenie") == ["uz"]
    assert contract_tags("", employment_types=["CONTRACTOR"]) == ["b2b"]


class _Site(httpx.BaseTransport):
    def __init__(self):
        self.calls: list[str] = []

    def handle_request(self, request):
        url = str(request.url)
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        if url.endswith("/sitemap.xml"):
            return httpx.Response(200, text=INDEX)
        if url.endswith("/jobs.xml.gz"):
            return httpx.Response(200, content=gzip.compress(URLSET.encode()))
        if "/job/1-" in url:
            return httpx.Response(200, text=_page(JP), headers={"content-type": "text/html"})
        return httpx.Response(404)


def test_adapter_end_to_end_with_robots():
    site = _Site()
    spec = SourceSpec(id="ex", adapter="sitemap_jsonld", params={
        "sitemap_index": "https://ex.test/sitemap.xml", "sitemap_include": "jobs",
        "id_regex": r"/(?P<id>\d+)-", "slug_regex": r"/\d+-(?P<slug>[^/?#]+)", "skip_expired": False,
        "url_exclude": ["java"],
    })
    with PoliteClient("ex", transport=site, sleep=False, respect_robots=True, max_requests=20) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=lambda t: "data" in t or "bi" in t)
        posts = list(get_adapter("sitemap_jsonld")(ctx))
    assert [p.source_job_id for p in posts] == ["1"]
    assert not any("blog.xml" in c or "/private/" in c or "java" in c for c in site.calls)
    assert any("robots=1" in n for n in ctx.notes)


def test_newest_id_first_without_lastmod():
    # revpath-style sitemap: no lastmod, numeric ids grow with recency, many jobs share a title slug
    urlset = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
              + "".join(f"<url><loc>https://rp.test/jobs/{i}-revenue-operations-manager</loc></url>" for i in (5, 900, 70))
              + "</urlset>")
    calls: list[str] = []

    def handle(request):
        url = str(request.url)
        calls.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nDisallow: /rss/\n")
        if url.endswith("/jobs.xml"):
            return httpx.Response(200, text=urlset)
        return httpx.Response(200, text=_page({**JP, "title": "Revenue Operations Manager"}),
                              headers={"content-type": "text/html"})

    spec = SourceSpec(id="rp", adapter="sitemap_jsonld", params={
        "sitemaps": "https://rp.test/jobs.xml", "id_regex": r"/jobs/(?P<id>\d+)-", "slug_strip": [r"^\d+-"],
        "newest_id_first": True, "max_details": 2, "skip_expired": False,
    })
    with PoliteClient("rp", transport=httpx.MockTransport(handle), sleep=False, respect_robots=True,
                      max_requests=20) as http:
        ctx = FetchContext(spec=spec, http=http, wants_detail=lambda t: t == "revenue operations manager")
        posts = list(get_adapter("sitemap_jsonld")(ctx))
    assert [p.source_job_id for p in posts] == ["900", "70"]
