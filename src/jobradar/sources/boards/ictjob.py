"""ICTjob.be — Belgian IT job board (server-rendered HTML with schema.org microdata).

List:   GET https://www.ictjob.be/en/search-it-jobs?keywords=<q>&From=<20*k>&To=<20*k+19>
        20 ``li.search-item`` per request (hard cap; ``?page=N`` is ignored, the site pages through
        ``/ajax/``, which robots.txt disallows, but ``From``/``To`` work on the normal page). Rows carry
        title, company, city, "dd/mm" posting date and a skills line; the result count is in the page.
Detail: GET /en/job/<slug>/<n>-<id> → ``div.content[itemprop=description]`` (HTML, often Dutch/French),
        workArrangement, employmentType and the ``languageRequirements`` block (language + required level).
        Fetched only for titles passing ``ctx.wants_detail`` that are not stored yet.
robots.txt: everything except ``/*/ajax/`` and the legal pages.

Language mapping: the structured requirements are appended to the description in words the matcher's
``language_requirements`` understands ("French required." / "Dutch is a plus."). When English is also listed
joined by "or" (alternatives) the other languages are written as "a plus", not "required".

Params: queries (default: the role packs' search terms), max_pages 3,
        max_details 25.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterator

from bs4 import BeautifulSoup

from jobradar.http.client import Blocked, BudgetExceeded
from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, default_terms, as_list

SITE = "https://www.ictjob.be"
PAGE = 20
_ID = re.compile(r"/job/[^/]+/([\w-]+)")
_DATE = re.compile(r"(\d{1,2})/(\d{1,2})")


def _txt(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def _posted(s: str) -> datetime | None:
    m = _DATE.search(s)
    if not m:
        return None
    now = datetime.now(timezone.utc)
    try:
        d = datetime(now.year, int(m.group(2)), int(m.group(1)), tzinfo=timezone.utc)
    except ValueError:
        return None
    return d.replace(year=now.year - 1) if d > now else d


def parse_list(html: str, source: str) -> tuple[list[Posting], int]:
    soup = BeautifulSoup(html, "lxml")
    out: list[Posting] = []
    for li in soup.select("li.search-item"):
        a = li.select_one("a.search-item-link")
        href = (a.get("href") if a else "") or ""
        m = _ID.search(href)
        if not m:
            continue
        city = _txt(li.select_one('[itemprop="addressLocality"]'))
        out.append(Posting(
            source=source,
            source_job_id=m.group(1),
            title=_txt(a.select_one("h2")) or _txt(a),
            company=_txt(li.select_one(".job-company")),
            url=href.split("?")[0],
            location_raw=city or "Belgium",
            cities=[city] if city else [],
            posted_at=_posted(_txt(li.select_one(".job-date"))),
            description=_txt(li.select_one(".job-keywords")),
            tags=["ictjob:list"],
        ))
    total = re.search(r"numResult=(\d+)", html)
    return out, int(total.group(1)) if total else 0


def _language_text(soup: BeautifulSoup) -> str:
    box = soup.select_one('[itemprop="languageRequirements"]')
    if not box:
        return ""
    items = []  # (name, basic?, joined-by-or?)
    or_next = False
    for el in box.find_all("span", recursive=False):
        cls = el.get("class") or []
        if "job-language-proficiency-conjunction" in cls:
            or_next = _txt(el).lower() in ("or", "of", "ou")
        elif "job-language-name" in cls:
            level = (el.get("title") or "").lower()
            items.append([_txt(el), "basic" in level or "elementary" in level, or_next])
            or_next = False
    names = {n.lower() for n, _, _ in items}
    alternatives = "english" in names and any(o for _, _, o in items)
    sent = []
    for name, basic, _ in items:
        if name.lower() in ("english", "polish"):
            continue
        sent.append(f"{name} is a plus." if basic or alternatives else f"{name} required.")
    return " ".join(sent)


def parse_detail(post: Posting, html: str) -> None:
    soup = BeautifulSoup(html, "lxml")
    body = soup.select_one('div.content[itemprop="description"]')
    if body:
        for t in body.find_all(["script", "style"]):
            t.decompose()
        text = html_to_text(str(body))
        if text:
            post.description = text
    lang = _language_text(soup)
    if lang:
        post.description = f"{post.description}\n\nLanguages: {lang}"
    arr = _txt(soup.select_one("#work-arrangement")).lower()
    if "hybrid" in arr:
        post.workplace = "hybrid"
    elif "remote" in arr or "home" in arr:
        post.workplace = "remote"
    elif "site" in arr or "office" in arr:
        post.workplace = "onsite"
    emp = _txt(soup.select_one('[itemprop="employmentType"]'))
    if emp:
        post.employment_types = [emp]
    post.tags = [t for t in post.tags if t != "ictjob:list"]


@adapter("ictjob_be")
def ictjob_be(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    max_pages = int(p.get("max_pages", 3))
    max_details = int(p.get("max_details", 25))
    seen: set[str] = set()
    pending: list[Posting] = []
    stopped = False
    for q in as_list(p.get("queries") or default_terms(5)):
        for page in range(max_pages):
            try:
                resp = ctx.http.get(f"{SITE}/en/search-it-jobs",
                                    params={"keywords": q, "From": page * PAGE, "To": page * PAGE + PAGE - 1})
            except (Blocked, BudgetExceeded) as exc:
                ctx.notes.append(f"list stopped: {exc}")
                stopped = True
                break
            if resp.status_code != 200:
                ctx.notes.append(f"list HTTP {resp.status_code}")
                break
            rows, total = parse_list(resp.text, ctx.spec.id)
            relevant = 0
            for post in rows:
                if post.source_job_id in seen:
                    relevant += 1  # already counted under another query; keeps paging decision honest
                    continue
                seen.add(post.source_job_id)
                if not ctx.wants_detail(post.title):
                    ctx.skipped += 1
                    continue
                relevant += 1
                if ctx.is_known(ctx.spec.id, post.source_job_id):
                    ctx.mark_seen(ctx.spec.id, post.source_job_id)
                else:
                    pending.append(post)
            if len(rows) < PAGE or (page + 1) * PAGE >= total or relevant == 0:
                break
        if stopped:
            break
    blocked = False
    for i, post in enumerate(pending):
        if not blocked and i < max_details:
            try:
                resp = ctx.http.get(post.url)
                if resp.status_code == 200:
                    parse_detail(post, resp.text)
                elif resp.status_code in (404, 410):
                    continue
            except (Blocked, BudgetExceeded) as exc:
                blocked = True
                ctx.notes.append(f"detail stopped: {exc}")
        yield post
