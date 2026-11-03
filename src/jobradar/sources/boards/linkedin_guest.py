"""LinkedIn public "guest" job-search fragments (LOW VOLUME, permission-gated).

linkedin.com/robots.txt disallows ``/jobs-guest/`` for every crawler (and ``User-agent: *
Disallow: /``), and its header says automated access needs LinkedIn's express permission.
The adapter therefore checks robots.txt itself before every request and refuses (raises
``Blocked``) unless ``params.permission_granted: true`` is set deliberately. Parsing is
kept so the source can be switched on if permission is ever obtained.
"""

from __future__ import annotations

import re
from typing import Iterator
from urllib.parse import quote, urlencode

from bs4 import BeautifulSoup

from jobradar.http.client import Blocked
from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, default_terms, parse_date

LI_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LI_DETAIL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
_WT = {"1": "onsite", "2": "remote", "3": "hybrid"}
_ID = re.compile(r"(?:jobPosting:|-|/view/)(\d{6,})")


def parse_cards(html: str) -> list[dict[str, str | None]]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for card in soup.select("div.base-card, div.base-search-card, li > div[data-entity-urn]"):
        urn = card.get("data-entity-urn") or ""
        link = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
        href = (link.get("href") if link else "") or ""
        m = _ID.search(urn) or _ID.search(href.split("?")[0])
        if not m:
            continue
        title = card.select_one(".base-search-card__title")
        company = card.select_one(".base-search-card__subtitle")
        loc = card.select_one(".job-search-card__location")
        when = card.select_one("time")
        out.append({
            "id": m.group(1),
            "title": title.get_text(" ", strip=True) if title else "",
            "company": company.get_text(" ", strip=True) if company else "",
            "location": loc.get_text(" ", strip=True) if loc else "",
            "datetime": when.get("datetime") if when else None,
            "url": f"https://www.linkedin.com/jobs/view/{m.group(1)}/",
        })
    return out


def _guard(ctx: FetchContext, url: str) -> None:
    if ctx.params.get("permission_granted") is True:
        return
    if not ctx.http.allowed(url):
        raise Blocked(url, "robots.txt disallows (set permission_granted only with LinkedIn's permission)")


@adapter("linkedin_guest")
def linkedin_guest(ctx: FetchContext) -> Iterator[Posting]:
    """LinkedIn's logged-out job search pages.

    At most ``max_searches`` (≤3) searches × 1 page; descriptions off by default."""
    p = ctx.params
    searches = (p.get("searches") or [
        {"keywords": k, "location": "European Union", "f_WT": "2"} for k in default_terms(3)
    ])[: min(int(p.get("max_searches", 3)), 3)]
    fetch_desc = bool(p.get("fetch_descriptions", False))
    max_desc = min(int(p.get("max_descriptions", 5)), 10)
    seen: set[str] = set()
    described = 0
    for s in searches:
        q = {"keywords": s.get("keywords"), "location": s.get("location"), "f_WT": s.get("f_WT"),
             "f_TPR": s.get("f_TPR", p.get("f_TPR", "r86400")), "start": 0}
        url = f"{LI_SEARCH}?{urlencode({k: v for k, v in q.items() if v not in (None, '')}, quote_via=quote)}"
        _guard(ctx, url)
        resp = ctx.http.get(url)  # 403/429 raise Blocked → the run stops immediately
        if resp.status_code != 200:
            ctx.notes.append(f"search HTTP {resp.status_code}")
            break
        wt = str(s.get("f_WT") or "")
        workplace = _WT.get(wt) if wt in _WT else None  # only when one mode was filtered
        for c in parse_cards(resp.text):
            jid = str(c["id"])
            if jid in seen:
                continue
            seen.add(jid)
            desc = ""
            if fetch_desc and described < max_desc and ctx.wants_detail(c["title"] or ""):
                durl = LI_DETAIL.format(id=jid)
                _guard(ctx, durl)
                d = ctx.http.get(durl)
                described += 1
                if d.status_code == 200:
                    node = BeautifulSoup(d.text, "lxml").select_one(".show-more-less-html__markup, .description__text")
                    desc = html_to_text(str(node)) if node else ""
            yield Posting(
                source=ctx.spec.id,
                source_job_id=jid,
                title=c["title"] or "",
                company=c["company"] or "",
                url=c["url"] or "",
                location_raw=c["location"] or "",
                workplace=workplace,
                description=desc,
                posted_at=parse_date(c["datetime"]),
                raw={"search": {"keywords": s.get("keywords"), "location": s.get("location"), "f_WT": wt}},
            )
