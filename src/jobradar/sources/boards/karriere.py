"""karriere.at (Austria) — the site's own JSON search endpoint + schema.org JobPosting on detail pages.

robots.txt allows everything for ``*``.

1. GET https://www.karriere.at/jobs?keywords=<q>&page=N with ``Accept: application/json`` — the call
   the "load more jobs" button makes (controller/jobsSearch.js) → ``data.jobsSearchList.activeItems``
   (15 per page: id, title, company, locations, isHomeOffice, employmentTypes, salary text, relative
   German date, summary) and ``loadMoreJobsButton.next``. Without the Accept header the same URL
   returns the server-rendered HTML page; ``parse_list`` handles that as a fallback.
2. GET https://www.karriere.at/jobs/<id> → JSON-LD JobPosting (full description, address, salary)
   for titles passing ``ctx.wants_detail`` that are not stored yet (``max_details``).

``isHomeOffice`` is the site's structured "home office possible" flag → workplace hybrid.

Params: keywords (default: the role packs' search terms), max_pages 3,
        max_details 20.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterator

from bs4 import BeautifulSoup

from jobradar.http.client import Blocked, BudgetExceeded
from jobradar.models import Posting
from jobradar.sources.base import FetchContext, adapter, default_terms, as_list, parse_date
from jobradar.sources.generic.rss import merge_jobposting

SITE = "https://www.karriere.at"
_ID = re.compile(r"/jobs/(\d+)")
_REL = re.compile(r"vor\s+(\d+)\s+(minute|stunde|tag|woche|monat)", re.I)
_NUM = re.compile(r"\d{1,3}(?:\.\d{3})*(?:,\d+)?|\d+(?:,\d+)?")


def german_date(text: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(timezone.utc)
    low = (text or "").lower()
    if "heute" in low:
        return now
    if "gestern" in low:
        return now - timedelta(days=1)
    m = _REL.search(low)
    if m:
        n = int(m.group(1))
        days = {"minute": 1 / 1440, "stunde": 1 / 24, "tag": 1, "woche": 7, "monat": 30}[m.group(2)]
        return now - timedelta(days=n * days)
    m = re.search(r"\d{1,2}\.\d{1,2}\.\d{4}", low)
    return parse_date(m.group(0)) if m else None


def parse_salary(text: str) -> tuple[float | None, float | None, str | None, str | None]:
    """'3.500 € – 4.500 € monatlich' → (3500, 4500, 'EUR', 'month'); 'ab 50.000 € jährlich' → (50000, None, ...)."""
    if "€" not in text:
        return None, None, None, None
    nums = [float(n.replace(".", "").replace(",", ".")) for n in _NUM.findall(text)]
    if not nums:
        return None, None, None, None
    low = text.lower()
    period = "month" if "monat" in low else "year" if "jähr" in low or "jahr" in low else "hour" if "stund" in low else None
    smin = nums[0]
    smax = nums[1] if len(nums) > 1 else None
    return smin, smax, "EUR", period


def parse_list(page: str, source: str, now: datetime | None = None) -> list[Posting]:
    soup = BeautifulSoup(page, "lxml")
    out: list[Posting] = []
    for li in soup.select("li.m-jobsList__item"):
        a = li.select_one("a.m-jobsListItem__titleLink")
        if not a or not a.get("href"):
            continue
        m = _ID.search(a["href"])
        if not m:
            continue
        jid = m.group(1)
        company = li.select_one(".m-jobsListItem__companyName")
        date = li.select_one(".m-jobsListItem__date")
        locs = [x.get_text(" ", strip=True).rstrip(", ").strip() for x in li.select("a.m-jobsListItem__location")]
        pills = [x.get_text(" ", strip=True) for x in li.select(".m-jobsListItem__pill:not(.m-jobsListItem__locations)")]
        pills = [x for x in pills if x]
        summary = li.select_one(".m-jobListSummary__text")
        salary = next((x for x in pills if "€" in x), "")
        smin, smax, cur, period = parse_salary(salary)
        employment = [x for x in pills if x not in (salary,) and "homeoffice" not in x.lower()]
        homeoffice = any("homeoffice" in x.lower() for x in pills)
        out.append(Posting(
            source=source,
            source_job_id=jid,
            title=a.get_text(" ", strip=True),
            company=company.get_text(" ", strip=True) if company else "",
            url=f"{SITE}/jobs/{jid}",
            location_raw=", ".join(locs),
            countries=[],  # list items carry place names only; detail JSON-LD / default_country=AT fill it
            cities=locs,
            workplace="hybrid" if homeoffice else None,
            description=summary.get_text(" ", strip=True) if summary else "",
            posted_at=german_date(date.get_text(" ", strip=True), now) if date else None,
            employment_types=[e.strip() for x in employment for e in x.split(",") if e.strip()],
            salary_min=smin,
            salary_max=smax,
            salary_currency=cur,
            salary_period=period,
            tags=["homeoffice"] if homeoffice else [],
            raw={"pills": pills},
        ))
    return out


def item_to_posting(source: str, j: dict, now: datetime | None = None) -> Posting | None:
    jid = str(j.get("id") or "")
    if not jid or not j.get("title"):
        return None
    company = j.get("company") or {}
    locs = [str(x.get("name")).strip() for x in as_list(j.get("locations")) if isinstance(x, dict) and x.get("name")]
    salary = str(j.get("salary") or "")
    smin, smax, cur, period = parse_salary(salary)
    home = bool(j.get("isHomeOffice"))
    return Posting(
        source=source,
        source_job_id=jid,
        title=str(j["title"]).strip(),
        company=str(company.get("name") or "").strip() if isinstance(company, dict) else "",
        url=j.get("link") or f"{SITE}/jobs/{jid}",
        location_raw=", ".join(locs),
        cities=locs,
        workplace="hybrid" if home else None,
        description=str(j.get("summary") or "").strip(),
        posted_at=german_date(str(j.get("date") or ""), now),
        employment_types=[e.strip() for e in str(j.get("employmentTypes") or "").split(",") if e.strip()],
        salary_min=smin,
        salary_max=smax,
        salary_currency=cur,
        salary_period=period,
        tags=["homeoffice"] if home else [],
        raw={"salary": salary or None, "company_slug": company.get("slug") if isinstance(company, dict) else None},
    )


def _page(ctx: FetchContext, keyword: str, page: int) -> tuple[list[Posting], bool]:
    """(postings, has_next) for one search page; JSON first, HTML fallback."""
    url = f"{SITE}/jobs"
    resp = ctx.http.get(url, params={"keywords": keyword, "page": page},
                        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
    if resp.status_code != 200:
        ctx.notes.append(f"{keyword} p{page}: HTTP {resp.status_code}")
        return [], False
    if "json" in resp.headers.get("content-type", ""):
        data = (resp.json() or {}).get("data") or {}
        lst = data.get("jobsSearchList") or {}
        items = [x.get("jobsItem") for x in ((lst.get("activeItems") or {}).get("items") or []) if x.get("jobsItem")]
        posts = [p for p in (item_to_posting(ctx.spec.id, j) for j in items) if p]
        nxt = (lst.get("loadMoreJobsButton") or {})
        return posts, bool(nxt.get("active") and nxt.get("next"))
    return parse_list(resp.text, ctx.spec.id), False


@adapter("karriere_at")
def karriere_at(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    max_details = int(p.get("max_details", 20))
    seen: set[str] = set()
    to_detail: list[Posting] = []
    stop = False
    for kw in as_list(p.get("keywords") or default_terms(4)):
        for page in range(1, int(p.get("max_pages", 3)) + 1):
            try:
                rows, has_next = _page(ctx, kw, page)
            except (Blocked, BudgetExceeded) as exc:
                ctx.notes.append(f"list stopped: {exc}")
                stop = True
                break
            except ValueError:
                ctx.notes.append(f"{kw} p{page}: bad JSON")
                break
            for post in rows:
                if post.source_job_id in seen:
                    continue
                seen.add(post.source_job_id)
                if not ctx.wants_detail(post.title):
                    yield post
                elif not ctx.is_known(ctx.spec.id, post.source_job_id):
                    to_detail.append(post)
            if not has_next:
                break
        if stop:
            break
    to_detail.sort(key=lambda x: x.posted_at.timestamp() if x.posted_at else 0, reverse=True)
    blocked = False
    for i, post in enumerate(to_detail):
        if not blocked and i < max_details:
            try:
                resp = ctx.http.get(post.url)
                if resp.status_code in (404, 410):
                    continue
                if resp.status_code == 200:
                    summary = post.description
                    post.description = ""  # prefer the full JSON-LD text over the list teaser
                    if not merge_jobposting(post, resp.text):
                        post.description = summary
                        ctx.notes.append(f"no JobPosting on {post.url}")
            except (Blocked, BudgetExceeded) as exc:
                blocked = True
                ctx.notes.append(f"detail stopped: {exc}")
        yield post
