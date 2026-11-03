"""RSS / XML job-feed adapter (rss_feed) and Jobindex — replayed fixtures + an inline <jobs><job> feed."""

from pathlib import Path

import httpx

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures"

JOB_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<jobs>
<job id="abc-W43">
  <id><![CDATA[abc-W43]]></id>
  <title><![CDATA[Senior Data Engineer (m/w/d)]]></title>
  <link><![CDATA[https://germantechjobs.de/jobs/Acme-GmbH-Senior-Data-Engineer]]></link>
  <country><![CDATA[Germany]]></country>
  <location><![CDATA[Torstrasse 1, Berlin]]></location>
  <city><![CDATA[Berlin]]></city>
  <salary><![CDATA[EUR 70'000 - 85'000 per year]]></salary>
  <company><![CDATA[Acme GmbH]]></company>
  <jobtype><![CDATA[Full-Time]]></jobtype>
  <pubdate><![CDATA[21.10.2026]]></pubdate>
  <description><![CDATA[<p>Build our <b>dbt</b> + BigQuery platform.</p>]]></description>
</job>
<job id="def-W43">
  <title><![CDATA[Frontend Developer]]></title>
  <link><![CDATA[https://germantechjobs.de/jobs/Other-AG-Frontend-Developer]]></link>
  <country><![CDATA[Switzerland]]></country>
  <city><![CDATA[Zürich]]></city>
  <company><![CDATA[Other AG]]></company>
  <pubdate><![CDATA[20.10.2026]]></pubdate>
  <description><![CDATA[<p>React.</p>]]></description>
</job>
</jobs>""".encode()


def _run(spec, transport):
    with PoliteClient(spec.id, transport=transport, sleep=False, max_requests=50) as http:
        posts = list(get_adapter(spec.adapter)(FetchContext(spec=spec, http=http)))
        return posts, http.stats.requests


def test_job_feed_xml():
    url = "https://germantechjobs.de/job_feed.xml"
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=JOB_FEED) if str(req.url) == url
                                    else httpx.Response(404))
    spec = SourceSpec(id="germantechjobs", adapter="rss_feed", params={
        "feeds": [url], "id_from": "link", "company_field": "company",
        "location_field": ["city", "location"], "country_field": "country"})
    posts, requests = _run(spec, transport)
    assert requests == 1
    first, second = posts
    assert first.source_job_id == first.url == "https://germantechjobs.de/jobs/Acme-GmbH-Senior-Data-Engineer"
    assert (first.title, first.company, first.location_raw) == ("Senior Data Engineer (m/w/d)", "Acme GmbH", "Berlin")
    assert first.countries == ["DE"] and second.countries == ["CH"]
    assert first.posted_at.isoformat().startswith("2026-10-21")
    assert first.employment_types == ["Full-Time"]
    assert "dbt" in first.description and "<" not in first.description


def test_wordpress_feed_title_split():
    spec = SourceSpec(id="berlinstartupjobs", adapter="rss_feed", params={
        "feeds": ["https://berlinstartupjobs.com/engineering/feed/"],
        "title_split": {"seps": [" // "], "company": "right"}})
    posts, _ = _run(spec, ReplayTransport(FIX / "berlinstartupjobs"))
    assert len(posts) == len({p.source_job_id for p in posts}) >= 10
    assert all(p.title and p.company and " // " not in p.title for p in posts)
    assert all(p.posted_at and p.url.startswith("https://berlinstartupjobs.com/") for p in posts)


def test_jobindex():
    spec = SourceSpec(id="jobindex", adapter="jobindex", default_country="DK")
    posts, requests = _run(spec, ReplayTransport(FIX / "jobindex"))
    assert requests == 4  # one feed per default query; no paging (robots)
    assert len(posts) == len({p.source_job_id for p in posts}) >= 20
    assert all(p.title and p.url.startswith("https://www.jobindex.dk/") for p in posts)
    assert sum(bool(p.company) for p in posts) >= len(posts) - 3  # a few items carry no ", <company>" suffix


def test_refused_feed_raises_blocked_instead_of_returning_empty():
    """swissdevjobs 2026-10-28..30: Cloudflare 403 from GitHub IPs read as an "empty" run with no error."""
    import pytest

    from jobradar.http.client import Blocked

    url = "https://swissdevjobs.ch/job_feed.xml"
    spec = SourceSpec(id="swissdevjobs", adapter="rss_feed", params={"feeds": [url], "id_from": "link"})
    with pytest.raises(Blocked) as exc:
        _run(spec, httpx.MockTransport(lambda req: httpx.Response(403, text="<html>blocked</html>")))
    assert exc.value.status == 403


def test_one_refused_feed_among_working_ones_is_only_a_note():
    ok, bad = "https://germantechjobs.de/job_feed.xml", "https://example.org/feed.xml"
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=JOB_FEED) if str(req.url) == ok
                                    else httpx.Response(403))
    spec = SourceSpec(id="germantechjobs", adapter="rss_feed", params={"feeds": [bad, ok], "id_from": "link"})
    posts, _ = _run(spec, transport)
    assert len(posts) == 2
