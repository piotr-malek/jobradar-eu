"""Remote-job boards with free public JSON/RSS feeds.

Each adapter yields ``Posting`` objects; matching happens later. Attribution:
shortlist rows link back to the original posting URL on these boards.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import Iterator

from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, default_terms, as_list, first, parse_date, to_float


# ---------------------------------------------------------------- Arbeitnow
@adapter("arbeitnow")
def arbeitnow(ctx: FetchContext) -> Iterator[Posting]:
    url = "https://www.arbeitnow.com/api/job-board-api"
    for page in range(1, int(ctx.params.get("max_pages", 5)) + 1):
        data = ctx.http.get_json(url, params={"page": page})
        rows = data.get("data") or []
        for j in rows:
            yield Posting(
                source=ctx.spec.id,
                source_job_id=str(j.get("slug") or j.get("url")),
                title=j.get("title") or "",
                company=j.get("company_name") or "",
                url=j.get("url") or "",
                location_raw=j.get("location") or "",
                workplace="remote" if j.get("remote") else None,
                description=html_to_text(j.get("description")),
                posted_at=parse_date(j.get("created_at")),
                employment_types=as_list(j.get("job_types")),
                tags=as_list(j.get("tags")),
                raw={k: j.get(k) for k in ("slug", "remote", "location", "tags", "job_types", "created_at")},
            )
        if not rows or not (data.get("links") or {}).get("next"):
            break


# ----------------------------------------------------------------- Remotive
@adapter("remotive")
def remotive(ctx: FetchContext) -> Iterator[Posting]:
    """Remotive public API, one call per category or search term.

    Remotive asks for <= 4 calls/day; we make at most len(queries) (default 3)."""
    seen: set[str] = set()
    queries = [{"category": c} for c in as_list(ctx.params.get("categories"))]
    queries += [{"search": q} if isinstance(q, str) else q for q in (ctx.params.get("queries") or default_terms(3))]
    for q in queries:
        data = ctx.http.get_json("https://remotive.com/api/remote-jobs", params=q)
        for j in data.get("jobs") or []:
            jid = str(j.get("id"))
            if jid in seen:
                continue
            seen.add(jid)
            yield Posting(
                source=ctx.spec.id,
                source_job_id=jid,
                title=j.get("title") or "",
                company=j.get("company_name") or "",
                url=j.get("url") or "",
                workplace="remote",
                remote_scope=j.get("candidate_required_location") or None,
                location_raw=j.get("candidate_required_location") or "",
                description=html_to_text(j.get("description")),
                posted_at=parse_date(j.get("publication_date")),
                employment_types=as_list(j.get("job_type")),
                tags=as_list(j.get("tags")),
                raw={"salary": j.get("salary"), "category": j.get("category")},
            )


# ------------------------------------------------------------------- Jobicy
@adapter("jobicy")
def jobicy(ctx: FetchContext) -> Iterator[Posting]:
    seen: set[str] = set()
    for geo in ctx.params.get("geos", ["europe", "emea"]):
        for industry in ctx.params.get("industries", ["data-science", "engineering"]):
            data = ctx.http.get_json(
                "https://jobicy.com/api/v2/remote-jobs",
                params={"count": 100, "geo": geo, "industry": industry},
            )
            for j in data.get("jobs") or []:
                jid = str(j.get("id"))
                if jid in seen:
                    continue
                seen.add(jid)
                yield Posting(
                    source=ctx.spec.id,
                    source_job_id=jid,
                    title=html.unescape(j.get("jobTitle") or ""),
                    company=j.get("companyName") or "",
                    url=j.get("url") or "",
                    workplace="remote",
                    remote_scope=j.get("jobGeo") or None,
                    location_raw=j.get("jobGeo") or "",
                    description=html_to_text(j.get("jobDescription") or j.get("jobExcerpt")),
                    posted_at=parse_date(j.get("pubDate")),
                    employment_types=as_list(j.get("jobType")),
                    salary_min=to_float(j.get("annualSalaryMin")),
                    salary_max=to_float(j.get("annualSalaryMax")),
                    salary_currency=j.get("salaryCurrency"),
                    salary_period="year" if j.get("annualSalaryMin") else None,
                    tags=as_list(j.get("jobIndustry")),
                    raw={"jobLevel": j.get("jobLevel"), "jobGeo": j.get("jobGeo")},
                )


# ---------------------------------------------------------------- Himalayas
@adapter("himalayas")
def himalayas(ctx: FetchContext) -> Iterator[Posting]:
    seen: set[str] = set()
    pages = int(ctx.params.get("max_pages", 2))
    for q in ctx.params.get("queries") or default_terms(5):
        for page in range(1, pages + 1):
            data = ctx.http.get_json("https://himalayas.app/jobs/api/search", params={"q": q, "page": page})
            jobs = data.get("jobs") or []
            for j in jobs:
                jid = str(first(j.get("guid"), j.get("applicationLink"), j.get("title")))
                if jid in seen:
                    continue
                seen.add(jid)
                restrictions = [str(x) for x in as_list(j.get("locationRestrictions"))]
                yield Posting(
                    source=ctx.spec.id,
                    source_job_id=jid,
                    title=j.get("title") or "",
                    company=j.get("companyName") or "",
                    url=j.get("applicationLink") or j.get("guid") or "",
                    workplace="remote",
                    remote_scope=", ".join(restrictions) or "Worldwide",
                    location_raw=", ".join(restrictions),
                    description=html_to_text(j.get("description") or j.get("excerpt")),
                    posted_at=parse_date(j.get("pubDate")),
                    employment_types=as_list(j.get("employmentType")),
                    salary_min=to_float(j.get("minSalary")),
                    salary_max=to_float(j.get("maxSalary")),
                    salary_currency=j.get("currency"),
                    salary_period="year" if j.get("minSalary") else None,
                    tags=as_list(j.get("categories")) + as_list(j.get("seniority")),
                    raw={"timezoneRestrictions": j.get("timezoneRestrictions"), "seniority": j.get("seniority")},
                )
            if len(jobs) < 20:
                break


# ----------------------------------------------------------------- RemoteOK
@adapter("remoteok")
def remoteok(ctx: FetchContext) -> Iterator[Posting]:
    data = ctx.http.get_json("https://remoteok.com/api")
    for j in data:
        if not isinstance(j, dict) or not j.get("id") or not j.get("position"):
            continue  # first element is the legal notice
        yield Posting(
            source=ctx.spec.id,
            source_job_id=str(j["id"]),
            title=j.get("position") or "",
            company=j.get("company") or "",
            url=j.get("url") or "",
            apply_url=j.get("apply_url"),
            workplace="remote",
            remote_scope=j.get("location") or None,
            location_raw=j.get("location") or "",
            description=html_to_text(j.get("description")),
            posted_at=parse_date(j.get("epoch") or j.get("date")),
            salary_min=to_float(j.get("salary_min")),
            salary_max=to_float(j.get("salary_max")),
            salary_currency="USD" if j.get("salary_min") else None,
            salary_period="year" if j.get("salary_min") else None,
            tags=as_list(j.get("tags")),
        )


# ------------------------------------------------------ We Work Remotely RSS
WWR_FEEDS = (
    "remote-back-end-programming-jobs",
    "remote-full-stack-programming-jobs",
    "remote-devops-sysadmin-jobs",
    "remote-programming-jobs",
    "all-other-remote-jobs",
)


def _x(item: ET.Element, tag: str) -> str:
    node = item.find(tag)
    return (node.text or "").strip() if node is not None and node.text else ""


@adapter("weworkremotely")
def weworkremotely(ctx: FetchContext) -> Iterator[Posting]:
    seen: set[str] = set()
    for slug in ctx.params.get("feeds", WWR_FEEDS):
        resp = ctx.http.get(f"https://weworkremotely.com/categories/{slug}.rss")
        if resp.status_code != 200:
            ctx.notes.append(f"{slug}: HTTP {resp.status_code}")
            continue
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            ctx.notes.append(f"{slug}: unparseable RSS")
            continue
        for item in root.iter("item"):
            link = _x(item, "link") or _x(item, "guid")
            if not link or link in seen:
                continue
            seen.add(link)
            raw_title = _x(item, "title")
            company, _, title = raw_title.partition(":")
            if not title:
                company, title = "", raw_title
            region = ", ".join(x for x in (_x(item, "region"), _x(item, "country"), _x(item, "state")) if x)
            yield Posting(
                source=ctx.spec.id,
                source_job_id=link,
                title=title.strip(),
                company=company.strip(),
                url=link,
                workplace="remote",
                remote_scope=region or None,
                location_raw=region,
                description=html_to_text(_x(item, "description")),
                posted_at=parse_date(_x(item, "pubDate")),
                employment_types=as_list(_x(item, "type") or None),
                tags=[s.strip() for s in _x(item, "skills").split(",") if s.strip()],
            )


# ------------------------------------------------------------ Working Nomads
@adapter("workingnomads")
def workingnomads(ctx: FetchContext) -> Iterator[Posting]:
    data = ctx.http.get_json("https://www.workingnomads.com/api/exposed_jobs/")
    for j in data:
        url = j.get("url") or ""
        if not url:
            continue
        yield Posting(
            source=ctx.spec.id,
            source_job_id=url,
            title=j.get("title") or "",
            company=j.get("company_name") or "",
            url=url,
            workplace="remote",
            remote_scope=j.get("location") or None,
            location_raw=j.get("location") or "",
            description=html_to_text(j.get("description")),
            posted_at=parse_date(j.get("pub_date")),
            tags=[t.strip() for t in str(j.get("tags") or "").split(",") if t.strip()]
            + as_list(j.get("category_name")),
        )


# ------------------------------------------------------- HN "Who is hiring?"
_FIELD_SPLIT = re.compile(r"\s*(?:\||—|–|•)\s*")
_ROLE_WORD = re.compile(r"engineer|developer|analyst|scientist|architect|lead|manager|designer|advocate|specialist", re.I)
_REMOTE_WORD = re.compile(r"\bremote\b|\bonsite\b|\bon-site\b|\bhybrid\b", re.I)


def _hn_text(raw: str) -> str:
    text = (raw or "").replace("<p>", "\n\n")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]{2,}", " ", html.unescape(text)).strip()


@adapter("hn_whoishiring")
def hn_whoishiring(ctx: FetchContext) -> Iterator[Posting]:
    data = ctx.http.get_json(
        "https://hn.algolia.com/api/v1/search_by_date",
        params={"tags": "story,author_whoishiring", "hitsPerPage": 20},
    )
    thread = next((h for h in data.get("hits") or [] if str(h.get("title", "")).lower().startswith("ask hn: who is hiring")), None)
    if not thread:
        ctx.notes.append("no 'Who is hiring?' thread found")
        return
    item = ctx.http.get_json(f"https://hn.algolia.com/api/v1/items/{thread['objectID']}")
    for child in (item.get("children") or [])[: int(ctx.params.get("max_comments", 600))]:
        text = _hn_text(str(child.get("text") or ""))
        if not text:
            continue
        first_line = text.split("\n", 1)[0].strip()
        parts = [p.strip() for p in _FIELD_SPLIT.split(first_line) if p.strip()]
        if len(parts) < 3 and not (len(parts) == 2 and "http" in text):
            continue
        company = parts[0]
        role = next((p for p in parts[1:] if _ROLE_WORD.search(p)), parts[1])
        location_bits = [p for p in parts[1:] if p is not role and not p.lower().startswith("http") and not _ROLE_WORD.search(p)]
        yield Posting(
            source=ctx.spec.id,
            source_job_id=str(child.get("id")),
            title=role[:200],
            company=company[:120],
            url=f"https://news.ycombinator.com/item?id={child.get('id')}",
            location_raw=", ".join(location_bits)[:300],
            workplace="remote" if re.search(r"\bremote\b", first_line, re.I) and not re.search(r"hybrid|onsite|on-site", first_line, re.I) else None,
            description=text[:20000],
            posted_at=parse_date(child.get("created_at")),
            raw={"first_line": first_line, "thread": thread.get("title")},
        )
