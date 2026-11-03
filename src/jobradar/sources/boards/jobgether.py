"""Jobgether official JSON API (agents endpoint; robots.txt explicitly allows ``/astroapi/ai/jobs.json``).

Crawl-delay is 2 s (rps 0.5). The listing has no description and the detail pages (``/offer/…``) answer
with a Cloudflare 403 (checked 2026-10-31; robots.txt itself allows them), so a posting's description is
built from the API's own fields (remote type, places, experience, contract, job functions, salary). The
MCP endpoint (``/mcp``) has only ``searchJobs``, with the same fields.
"""

from __future__ import annotations

import re
from typing import Iterator
from urllib.parse import urlencode

from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, default_terms, parse_date, to_float
from jobradar.sources.boards.aggregators import SourceError
from jobradar.sources.boards.national_apis import country_iso

JOBGETHER_URL = "https://jobgether.com/astroapi/ai/jobs.json"
JOBGETHER_LOCATIONS = ["europe", "poland"]
_SALARY = re.compile(r"([\d.]+)\s*-\s*([\d.]+)\s*([A-Z]{3})?")


def listing_text(j: dict) -> str:
    """What the API says about a job, as text for the matcher and the screen (Jobgether has no posting text)."""
    parts = [("Work", j.get("remote")), ("Locations", j.get("location")), ("Experience", j.get("experience")),
             ("Contract", j.get("contractType")), ("Job functions", ", ".join(j.get("jobFunctions") or [])),
             ("Salary", j.get("salaryRange"))]
    lines = [f"{k}: {html_to_text(str(v))}." for k, v in parts if v]
    return " ".join(["Jobgether listing; the full posting text is not available."] + lines) if lines else ""


@adapter("jobgether")
def jobgether(ctx: FetchContext) -> Iterator[Posting]:
    """Jobgether's public API for agents (remote-friendly jobs by country).

    GET /astroapi/ai/jobs.json?keyword&locations=<slug>&limit=25&page=N&sort=date.

    Page 1 of every keyword × location slice first, then page 2, until ``max_calls`` (default 30).
    Jobgether lists one row per city for the same job, so rows are de-duplicated on
    (company, title) within the run. ``company`` is the hiring company; the agency flag comes from the
    ``jobgether`` marker in matching.yaml (source name), not the company field.
    """
    p = ctx.params
    keywords = p.get("keywords") or default_terms(8)
    locations = p.get("locations") or JOBGETHER_LOCATIONS
    max_pages = int(p.get("max_pages", 2))
    max_calls = int(p.get("max_calls", 30))
    limit = min(int(p.get("limit", 25)), 25)
    calls = 0
    seen: set[str] = set()
    seen_title: set[tuple[str, str]] = set()
    more = {(loc, kw): True for loc in locations for kw in keywords}
    for page in range(1, max_pages + 1):
        for loc in locations:
            for kw in keywords:
                if not more[(loc, kw)]:
                    continue
                if calls >= max_calls:
                    ctx.notes.append(f"max_calls {max_calls} reached")
                    return
                params = {"keyword": kw, "locations": loc, "limit": limit, "page": page, "sort": "date"}
                if p.get("include_hybrid"):
                    params["includeHybrid"] = "true"
                resp = ctx.http.get(f"{JOBGETHER_URL}?{urlencode(params)}")
                calls += 1
                if resp.status_code >= 400:
                    raise SourceError(f"HTTP {resp.status_code}")
                try:
                    data = resp.json()
                except ValueError:
                    raise SourceError("non-JSON response") from None
                more[(loc, kw)] = bool((data.get("pagination") or {}).get("hasMore"))
                for j in data.get("jobs") or []:
                    jid = str(j.get("id") or "")
                    if not jid or jid in seen:
                        continue
                    seen.add(jid)
                    title = html_to_text(j.get("title"))
                    company = j.get("company") or ""
                    tkey = (company.casefold(), title.casefold())
                    if tkey in seen_title:  # same job listed for several cities
                        continue
                    seen_title.add(tkey)
                    if not ctx.wants_detail(title):
                        ctx.skipped += 1
                        continue
                    place = j.get("location") or ""
                    isos = [c for c in dict.fromkeys(country_iso(x.strip()) for x in place.split(",")) if c]
                    remote = (j.get("remote") or "").lower()
                    sal = _SALARY.search(j.get("salaryRange") or "")
                    yield Posting(
                        source=ctx.spec.id,
                        source_job_id=jid,
                        title=title,
                        company=company,
                        url=j.get("url") or "",
                        location_raw=place,
                        countries=isos,
                        workplace="remote" if "remote" in remote else "hybrid" if "hybrid" in remote else None,
                        remote_scope=place or None,
                        description=listing_text(j),
                        posted_at=parse_date(j.get("postedAt")),
                        employment_types=[j["contractType"]] if j.get("contractType") else [],
                        salary_min=to_float(sal.group(1)) if sal else None,
                        salary_max=to_float(sal.group(2)) if sal else None,
                        salary_currency=(sal.group(3) if sal else None),
                        salary_period="year" if sal else None,
                        tags=list(j.get("jobFunctions") or []),
                        raw={"experience": j.get("experience"), "keyword": kw, "location_query": loc},
                    )
