"""Impactpool (https://www.impactpool.org): aggregator of UN / INGO jobs (UNICEF, WFP, UNDP, UNHCR, …).

Server-rendered Rails app, robots.txt allows everything. ``/search`` takes filters (``jf[]`` job function,
``s[]`` seniority, ``q`` free text, ``remote=1``) and ``per_page`` (up to 100), so a handful of list requests
return title + organisation + location + level for every data/IT/information-management job. Detail pages
(``/jobs/{id}``, no JSON-LD) add the description and are fetched only for ``ctx.wants_detail(title)``.
Purpose track: grade tags (``grade:P-3``) are added here, the matcher filters on them.

Params: job_functions [ids] (default IT 104, Documentation & Information Management 838, Statistics 65,
AI/ML 32381, Cyber 32382), seniority [ids] (default junior 1, mid 2, mid/senior 12, consultancy 14,
admin support 9: no senior/executive/internship/volunteer), queries [free-text q, optional extra searches],
per_page (100), max_pages (3 per search), max_details (35).
"""

from __future__ import annotations

import re
from typing import Iterator
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, parse_date
from jobradar.sources.boards.un_grades import grade_tag, is_non_job

BASE = "https://www.impactpool.org"
DEFAULT_FUNCTIONS = [104, 838, 65, 32381, 32382]
DEFAULT_SENIORITY = [1, 2, 12, 14, 9]
_REMOTE = re.compile(r"\b(remote|home[\s-]?based|telecommut\w*|work from home)\b", re.I)
_JOB_HREF = re.compile(r"^/jobs/(\d+)")


def _text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def parse_list(page: str) -> list[dict]:
    """Search results page → [{id, title, org, location, level}]."""
    soup = BeautifulSoup(page, "lxml")
    rows: list[dict] = []
    for card in soup.select("div.job"):
        a = card.find("a", href=_JOB_HREF)
        h3 = a.find("h3") if a else None
        if not a or not h3:
            continue
        cells = [_text(d) for d in a.select("div.ip-typography")]
        if len(cells) < 2:
            continue
        rows.append({
            "id": _JOB_HREF.match(a["href"]).group(1),
            "title": _text(h3),
            "org": cells[0],
            "location": cells[1] if len(cells) > 2 else "",
            "level": cells[-1] if len(cells) > 2 else "",
        })
    return rows


def parse_detail(page: str) -> dict:
    """Job page → {title, org, location, level, deadline, description}."""
    soup = BeautifulSoup(page, "lxml")
    box = soup.select_one("#job-description") or soup
    h1 = box.find("h1")
    spans = [_text(s) for s in box.select("div.ip-layout[wrap] > span.ip-typography")] if box else []
    deadline = re.search(r"Application deadline:\s*([A-Za-z]+ \d{1,2}, \d{4})", box.get_text(" ", strip=True))
    main = soup.select_one("div.main-content")
    return {
        "title": _text(h1),
        "org": spans[0] if spans else "",
        "meta": spans[1:],
        "deadline": deadline.group(1) if deadline else None,
        "description": html_to_text(str(main)) if main else "",
    }


def _search_url(params: list[tuple[str, object]]) -> str:
    return f"{BASE}/search?{urlencode(params)}"


@adapter("impactpool")
def impactpool(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    funcs = p.get("job_functions") or DEFAULT_FUNCTIONS
    seniority = p.get("seniority") or DEFAULT_SENIORITY
    queries = p.get("queries") or [None]
    per_page = int(p.get("per_page", 100))
    max_pages = int(p.get("max_pages", 3))
    max_details = int(p.get("max_details", 35))

    rows: dict[str, dict] = {}
    for q in queries:
        for page_no in range(1, max_pages + 1):
            params: list[tuple[str, object]] = [("jf[]", f) for f in funcs] + [("s[]", s) for s in seniority]
            if q:
                params.append(("q", q))
            params += [("page", page_no), ("per_page", per_page)]
            resp = ctx.http.get(_search_url(params))
            if resp.status_code != 200:
                ctx.notes.append(f"search page {page_no}: HTTP {resp.status_code}")
                break
            found = parse_list(resp.text)
            new = [r for r in found if r["id"] not in rows]
            rows.update({r["id"]: r for r in new})
            if len(found) < per_page or not new:
                break

    fetched = 0
    for r in rows.values():
        jid = r["id"]
        if is_non_job(r["level"]) or is_non_job(r["title"]):
            ctx.skipped += 1
            continue
        if ctx.is_known("impactpool", jid):
            ctx.mark_seen("impactpool", jid)
            continue
        tags = [t for t in [grade_tag(r["level"])] if t]
        loc = r["location"]
        post = Posting(
            source=ctx.spec.id, source_job_id=jid, title=r["title"], company=r["org"],
            url=f"{BASE}/jobs/{jid}", location_raw=loc, tags=tags,
            workplace="remote" if _REMOTE.search(f"{loc} {r['title']}") else None,
            raw={"level": r["level"]},
        )
        if fetched < max_details and ctx.http.remaining > 0 and ctx.wants_detail(r["title"]):
            fetched += 1
            resp = ctx.http.get(post.url)
            if resp.status_code == 200:
                d = parse_detail(resp.text)
                post.description = d["description"]
                if d["meta"]:
                    post.location_raw = d["meta"][0] or post.location_raw
                    if _REMOTE.search(" ".join(d["meta"][:1])):
                        post.workplace = "remote"
                if post.workplace is None and _REMOTE.search(post.description[:600]):
                    post.workplace = "remote"
                if d["deadline"]:
                    dl = parse_date(d["deadline"])
                    post.raw["validThrough"] = dl.isoformat() if dl else None
                post.raw["detail_meta"] = d["meta"]
                if not any(t.startswith("grade:") for t in post.tags):
                    for m in d["meta"]:
                        g = grade_tag(m)
                        if g:
                            post.tags.append(g)
                            break
        yield post
    ctx.notes.append(f"list_rows={len(rows)} detail_fetched={fetched}")
