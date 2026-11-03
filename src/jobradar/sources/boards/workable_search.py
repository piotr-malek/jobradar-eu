"""Workable cross-company job search (jobs.workable.com): one request returns ~20 full postings.

robots.txt only disallows ``/search*`` and ``/profile*``; the JSON API under ``/api/`` is not listed.
"""

from __future__ import annotations

from typing import Iterator
from urllib.parse import urlencode

from jobradar.http.client import Blocked
from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, default_terms, parse_date
from jobradar.sources.boards.aggregators import SourceError
from jobradar.sources.boards.national_apis import country_iso

WORKABLE_SEARCH_URL = "https://jobs.workable.com/api/v1/jobs"
WORKABLE_LOCATIONS = ["Europe", "Poland"]


@adapter("workable_search")
def workable_search(ctx: FetchContext) -> Iterator[Posting]:
    """jobs.workable.com, the cross-company search over every Workable career page.

    GET /api/v1/jobs?query&location=<country or continent name>[&workplace=remote][&pageToken].

    Plan: pages × locations × queries, page 1 of everything first, then page 2, until ``max_calls``
    (default 36). Page size is 20. Jobs carry the full HTML description, ``workplace`` and the
    company's website. The job URL is ``jobs.workable.com/view/<id>/...`` and the id is a Workable-wide
    UUID, not the account slug + shortcode, so ats_* fields cannot be derived and are left empty.
    """
    p = ctx.params
    queries = p.get("queries") or default_terms(8)
    locations = p.get("locations") or WORKABLE_LOCATIONS
    max_pages = int(p.get("max_pages", 2))
    max_calls = int(p.get("max_calls", 36))
    remote_only = bool(p.get("remote_only", False))
    calls = 0
    seen: set[str] = set()
    tokens: dict[tuple[str, str], str | None] = {(loc, q): "" for loc in locations for q in queries}
    for _page in range(max_pages):
        for loc in locations:
            for q in queries:
                token = tokens[(loc, q)]
                if token is None:  # no further page for this slice
                    continue
                if calls >= max_calls:
                    ctx.notes.append(f"max_calls {max_calls} reached")
                    return
                params = {"query": q, "location": loc}
                if remote_only:
                    params["workplace"] = "remote"
                if token:
                    params["pageToken"] = token
                try:
                    resp = ctx.http.get(f"{WORKABLE_SEARCH_URL}?{urlencode(params)}")
                except Blocked:
                    raise
                calls += 1
                if resp.status_code >= 400:
                    raise SourceError(f"HTTP {resp.status_code}")
                try:
                    data = resp.json()
                except ValueError:
                    raise SourceError("non-JSON response") from None
                tokens[(loc, q)] = data.get("nextPageToken") or None
                for j in data.get("jobs") or []:
                    jid = str(j.get("id") or "")
                    if not jid or jid in seen:
                        continue
                    seen.add(jid)
                    title = html_to_text(j.get("title"))
                    if not ctx.wants_detail(title):
                        ctx.skipped += 1
                        continue
                    loc_d = j.get("location") or {}
                    co = j.get("company") or {}
                    iso = country_iso(loc_d.get("countryName"))
                    place = ", ".join(x for x in (loc_d.get("city"), loc_d.get("subregion"), loc_d.get("countryName")) if x)
                    desc = "\n\n".join(html_to_text(j.get(k)) for k in ("description", "requirementsSection", "benefitsSection")
                                       if j.get(k))
                    yield Posting(
                        source=ctx.spec.id,
                        source_job_id=jid,
                        title=title,
                        company=co.get("title") or "",
                        url=j.get("url") or "",
                        location_raw=place,
                        countries=[iso] if iso else [],
                        cities=[loc_d["city"]] if loc_d.get("city") else [],
                        workplace=j.get("workplace") if j.get("workplace") in ("remote", "hybrid", "onsite") else None,
                        description=desc,
                        posted_at=parse_date(j.get("created")),
                        employment_types=[j["employmentType"]] if j.get("employmentType") else [],
                        tags=[x for x in (j.get("department"),) if x],
                        raw={"company_website": co.get("website"), "language": j.get("language"), "query": q,
                             "location_query": loc},
                    )
