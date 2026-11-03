"""The Hub (thehub.io) — Nordic/European startup jobs via the public JSON API its Nuxt frontend uses.

List:   GET https://api.thehub.io/v2/jobs?countryCode=<DK|SE|NO|FI|EU>&search=<q>&limit=50&page=N
        (``countryCode=REMOTE`` in the UI becomes ``isRemote=true``). Items carry title, company,
        location {locality, country}, isRemote — but no description or dates.
Detail: GET https://api.thehub.io/jobs/single/<id> → description (HTML), publishedAt, salary, link.
        Fetched only for titles passing ``ctx.wants_detail`` that are not stored yet (known relevant
        postings are skipped, as in nofluffjobs, so list-level rows never overwrite stored versions).
robots.txt (thehub.io) only disallows /startups/joblistwidget/.

Params: countries [EU, DK, SE, NO, FI, REMOTE], queries [data, analytics], limit 50, max_pages 2,
        max_details 25.
"""

from __future__ import annotations

from typing import Any, Iterator

from jobradar.http.client import Blocked, BudgetExceeded
from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, as_list, first, parse_date
from jobradar.sources.generic.sitemap_jsonld import country_iso2

API = "https://api.thehub.io"
SITE = "https://thehub.io"


def _location(loc: Any) -> tuple[str, list[str], list[str]]:
    if not isinstance(loc, dict):
        return "", [], []
    city = (loc.get("locality") or "").strip()
    country = (loc.get("country") or "").strip()
    iso = country_iso2(country) if country else None
    label = loc.get("address") or ", ".join(x for x in (city, country) if x)
    return label, [iso] if iso else [], [city] if city else []


def _merge_detail(post: Posting, d: dict[str, Any]) -> None:
    post.description = html_to_text(d.get("description")) or post.description
    post.posted_at = parse_date(first(d.get("publishedAt"), d.get("approvedAt"), d.get("createdAt")))
    link = d.get("link")
    if isinstance(link, str) and link.startswith("http"):
        post.apply_url = link
    salary = d.get("salary")
    if salary and salary not in ("competitive", "undisclosed"):
        post.tags.append(f"salary:{salary}")
    if d.get("equity") and d.get("equity") not in ("undisclosed", "none"):
        post.tags.append(f"equity:{d.get('equity')}")
    post.raw = {**(post.raw or {}), "expirationDate": d.get("expirationDate"), "scraped": d.get("scraped"),
                "hub": d.get("countryCode")}


@adapter("thehub")
def thehub(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    limit = int(p.get("limit", 50))
    max_pages = int(p.get("max_pages", 2))
    max_details = int(p.get("max_details", 25))
    seen: set[str] = set()
    to_detail: list[Posting] = []
    for country in as_list(p.get("countries", ["EU", "DK", "SE", "NO", "FI", "REMOTE"])):
        for query in as_list(p.get("queries", ["data", "analytics"])):
            for page in range(1, max_pages + 1):
                params: dict[str, Any] = {"search": query, "limit": limit, "page": page}
                if country == "REMOTE":
                    params["isRemote"] = "true"
                else:
                    params["countryCode"] = country
                try:
                    data = ctx.http.get_json(f"{API}/v2/jobs", params=params)
                except (Blocked, BudgetExceeded) as exc:
                    ctx.notes.append(f"list stopped: {exc}")
                    yield from _finish(ctx, to_detail, max_details)
                    return
                docs = data.get("docs") or []
                for j in docs:
                    jid = str(j.get("id") or "")
                    if not jid or jid in seen:
                        continue
                    seen.add(jid)
                    label, countries, cities = _location(j.get("location"))
                    remote = bool(j.get("isRemote"))
                    company = j.get("company") or {}
                    post = Posting(
                        source=ctx.spec.id,
                        source_job_id=jid,
                        title=(j.get("title") or "").strip(),
                        company=(company.get("name") or "").strip(),
                        url=f"{SITE}/jobs/{jid}",
                        location_raw="Remote" + (f" ({label})" if label else "") if remote else label,
                        countries=countries,
                        cities=cities,
                        workplace="remote" if remote else None,
                        tags=[f"hub:{country}"],
                        raw={"key": j.get("key"), "company_key": company.get("key"),
                             "employees": company.get("numberOfEmployees")},
                    )
                    if not ctx.wants_detail(post.title):
                        yield post  # title-excluded: list-level row is enough (drop audit)
                    elif not ctx.is_known(ctx.spec.id, jid):
                        to_detail.append(post)
                    # known + relevant: already stored with description; a list-level re-yield would churn versions
                if page >= int(data.get("pages") or 0) or len(docs) < limit:
                    break
    yield from _finish(ctx, to_detail, max_details)


def _finish(ctx: FetchContext, to_detail: list[Posting], max_details: int) -> Iterator[Posting]:
    blocked = False
    for i, post in enumerate(to_detail):
        if not blocked and i < max_details:
            try:
                resp = ctx.http.get(f"{API}/jobs/single/{post.source_job_id}")
                if resp.status_code == 200:
                    _merge_detail(post, resp.json())
                elif resp.status_code in (404, 410):
                    continue  # expired between list and detail
            except (Blocked, BudgetExceeded) as exc:
                blocked = True
                ctx.notes.append(f"detail stopped: {exc}")
            except ValueError:
                ctx.notes.append(f"bad detail JSON for {post.source_job_id}")
        yield post
    to_detail.clear()
