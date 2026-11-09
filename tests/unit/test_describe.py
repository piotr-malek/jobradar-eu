"""Description finder: short jobs get their text from state.db, the company ATS route or the page's JSON-LD; hosts
that forbid robots are never opened; the night's request cap holds."""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from jobradar.apply.prep import PrepHttp
from jobradar.apply.route import Route
from jobradar.pipeline.describe import describe
from jobradar.store.sqlite import SQLiteStore

LONG = "We are hiring a content marketer to run our blog and newsletter. " * 30


@dataclass
class It:
    job_id: str
    title: str
    company: str
    url: str
    description: str = ""


def _transport(seen):
    def handle(req):
        seen.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if req.url.host == "boards-api.greenhouse.io":
            return httpx.Response(200, json={"content": "&lt;p&gt;" + LONG + "&lt;/p&gt;", "questions": []})
        if req.url.host == "careers.example.com":
            ld = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Copywriter",
                  "description": "<p>" + LONG + "</p>", "hiringOrganization": {"name": "Inkwell"}}
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text=f'<html><script type="application/ld+json">{json.dumps(ld)}</script></html>')
        return httpx.Response(404)
    return httpx.MockTransport(handle)


def test_short_jobs_get_text_from_the_store_the_ats_and_the_page(tmp_path, monkeypatch):
    monkeypatch.setenv("JR_DESCRIBE_CAP", "10")
    store = SQLiteStore(tmp_path / "state.db")
    store.append("posting_versions", [{"posting_key": "pk1", "source": "ats_greenhouse", "version_hash": "v",
                                       "observed_at": "2026-11-09T00:00:00+00:00", "title": "Content Marketing Manager",
                                       "company": "Northwind", "company_norm": "northwind", "description": LONG}])
    items = [It("JR-1", "Content Marketing Manager (m/w/d)", "Northwind GmbH", "https://de.jooble.org/desc/1", "Short."),
             It("JR-2", "SEO Specialist", "Acme", "https://boards.greenhouse.io/acme/jobs/42"),
             It("JR-3", "Copywriter", "Inkwell", "https://careers.example.com/jobs/7", "Snippet"),
             It("JR-4", "Brand Manager", "Zed", "https://www.linkedin.com/jobs/view/9"),
             It("JR-5", "PR Specialist", "Done", "https://x.example/1", LONG)]
    routes = {"JR-2": Route("greenhouse", "https://boards.greenhouse.io/acme/jobs/42", slug="acme", job="42")}
    seen: list[str] = []
    with PrepHttp(transport=_transport(seen), sleep=False) as http:
        out = describe(store, items, routes, http=http)
    texts = {it.job_id: it.description for it in items}
    assert texts["JR-1"] == LONG                                          # same company, similar title, in state.db
    assert "content marketer" in texts["JR-2"] and "content marketer" in texts["JR-3"]
    assert texts["JR-4"] == ""                                            # LinkedIn is never opened
    assert out["found"] == {"store": 1, "route": 1, "page": 1}
    assert out["before"] == {"full": 1, "snippet": 2, "none": 2} and out["after"]["full"] == 4
    assert not any("jooble" in u or "linkedin" in u for u in seen)


def test_the_request_cap_holds(tmp_path, monkeypatch):
    monkeypatch.setenv("JR_DESCRIBE_CAP", "2")
    store = SQLiteStore(tmp_path / "state.db")
    store.read("nights")
    items = [It(f"JR-{i}", "Copywriter", f"Co{i}", f"https://careers.example.com/jobs/{i}") for i in range(5)]
    seen: list[str] = []
    with PrepHttp(transport=_transport(seen), sleep=False) as http:
        out = describe(store, items, {}, http=http)
    assert out["opened"] == 2 and out["found"]["page"] == 2
    assert sum(1 for it in items if it.description) == 2
