"""Read a job posting the user found by hand, without an LLM: role, company, location, salary and posting text.

One polite look at the page (robots respected): schema.org JobPosting JSON-LD first (most boards and ATS pages
carry it for Google for Jobs), else ``og:title`` / ``og:site_name`` and the page's main text, else the ATS slug.
The application route comes from the URL or an ATS link on the page; a readable ATS (Greenhouse, Ashby, Recruitee)
also gives the posting text through its API.
Pages we may not fetch (LinkedIn's robots.txt) give nothing, and the user types role and company in.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from bs4 import BeautifulSoup

from jobradar.apply.forms import fetch_form
from jobradar.apply.route import Route, route_for_url, route_in_page
from jobradar.pipeline.shortlist import fmt_salary
from jobradar.http.client import Blocked, BudgetExceeded
from jobradar.normalize.text import html_to_text
from jobradar.sources.generic.sitemap_jsonld import extract_jobposting, jobposting_to_posting

log = logging.getLogger(__name__)

PAGE_TEXT_MIN = 300          # shorter "main text" is a JS shell or a cookie banner, not a posting
PAGE_TEXT_MAX = 20_000


def read_posting(url: str, http: Any) -> dict[str, Any]:
    """``http`` is a ``PrepHttp``. Never raises; ``note`` says why a field is empty."""
    out: dict[str, Any] = {"title": "", "company": "", "location": "", "salary": "", "description": "",
                           "route": route_for_url(url, "manual") or Route("unknown", url, via="manual"), "note": ""}
    try:
        resp = http.web.get(url)
    except (Blocked, BudgetExceeded) as exc:
        out["note"] = "the site doesn't allow automated reads" if isinstance(exc, Blocked) else "request budget used up"
        resp = None
    except Exception as exc:  # noqa: BLE001 - the user can still type the fields in
        log.info("posting read failed: %s", type(exc).__name__)
        out["note"] = f"couldn't open the page ({type(exc).__name__})"
        resp = None
    if resp is not None and resp.status_code >= 400:
        out["note"] = f"the page answered {resp.status_code}"
    elif resp is not None and "html" in resp.headers.get("content-type", "html"):
        _from_page(out, resp)
    route = out["route"]
    if not out["description"] and route.readable:
        spec = fetch_form(route.vendor, route.slug, route.job, http.for_vendor(route.vendor))
        out["description"] = (spec.content if spec else "") or ""
    if not out["company"] and len(route.slug) >= 3:          # Workable's /j/<code> links parse as slug "j"
        out["company"] = route.slug.replace("-", " ").replace("_", " ").title()
    return out


def _from_page(out: dict[str, Any], resp: Any) -> None:
    if out["route"].vendor in ("unknown", "linkedin"):
        out["route"] = route_in_page(resp, "manual") or out["route"]
    jp = extract_jobposting(resp.text)
    if jp:
        p = jobposting_to_posting(jp, source="manual", source_job_id="", url=str(resp.url))
        out.update(title=p.title, company=p.company, location=p.location_raw or "", description=p.description,
                   salary=fmt_salary(asdict(p)) or "")
    if out["title"] and out["company"] and len(out["description"]) >= PAGE_TEXT_MIN:
        return
    soup = BeautifulSoup(resp.text, "lxml")
    og, site = soup.find("meta", property="og:title"), soup.find("meta", property="og:site_name")
    if not out["company"] and site and site.get("content"):
        out["company"] = site["content"].strip()            # careers pages: "Abnormal AI", "Bosch Group"
    if not out["title"]:
        out["title"] = ((og.get("content") if og else "") or (soup.title.string if soup.title else "") or "").strip()
    if len(out["description"]) < PAGE_TEXT_MIN:
        for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form"]):
            tag.decompose()
        main = soup.find("main") or soup.find("article") or soup.body
        text = html_to_text(str(main)) if main else ""
        if len(text) >= PAGE_TEXT_MIN:
            out["description"] = text[:PAGE_TEXT_MAX]
