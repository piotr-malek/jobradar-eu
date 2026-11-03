"""Jobindex.dk (Denmark) — public RSS search feed.

GET https://www.jobindex.dk/jobsoegning.rss?q=<query> → 20 newest matches. Unquoted multi-word
queries are OR searches ("mindst et af ordene"), so phrases are sent quoted. robots.txt disallows
search URLs with two or more '+' (``/jobsoegning*?*+*+``), paging/sorting params and a few others,
so each query must be at most two words and no paging is used.

Item title is "<job title>, <company>"; the description HTML holds the area
(``<span class="jix_robotjob--area">``) and a teaser.

Params: queries (default: the role packs' search terms).
"""

from __future__ import annotations

import re
from typing import Iterator
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from jobradar.http.client import Blocked, BudgetExceeded
from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, default_terms, as_list, parse_date
from jobradar.sources.generic.rss import parse_feed, split_title

FEED = "https://www.jobindex.dk/jobsoegning.rss"
_ID = re.compile(r"/(?:vis-job/)?([a-z]?\d+)/?$")


def item_to_posting(source: str, rec: dict) -> Posting | None:
    link = (rec.get("link") or "").strip()
    if not link:
        return None
    title, company = split_title(rec.get("title", ""), {"seps": [", "], "company": "right"})
    body = rec.get("description") or ""
    soup = BeautifulSoup(body, "lxml") if "<" in body else None
    areas = [s.get_text(" ", strip=True) for s in soup.select(".jix_robotjob--area")] if soup else []
    if soup:
        for tag in soup.select("h4, .jix_robotjob--area, center, img"):
            tag.decompose()
        text = html_to_text(str(soup))
    else:
        text = html_to_text(body)
    m = _ID.search(rec.get("guid") or link)
    return Posting(
        source=source,
        source_job_id=m.group(1) if m else link,
        title=title.replace("​", ""),
        company=company,
        url=link,
        location_raw=", ".join(a for a in areas if a),
        cities=[a for a in areas if a],
        description=text,
        posted_at=parse_date(rec.get("pubdate")),
        tags=list(rec.get("categories") or []),
    )


@adapter("jobindex")
def jobindex(ctx: FetchContext) -> Iterator[Posting]:
    seen: set[str] = set()
    for q in as_list(ctx.params.get("queries") or default_terms(4)):
        if q.count(" ") > 1:
            ctx.notes.append(f"skipped {q!r}: robots.txt disallows queries with 2+ '+'")
            continue
        url = f"{FEED}?q={quote_plus(q)}"
        try:
            resp = ctx.http.get(url)
        except (Blocked, BudgetExceeded) as exc:
            ctx.notes.append(f"stopped: {exc}")
            return
        if resp.status_code != 200:
            ctx.notes.append(f"{url}: HTTP {resp.status_code}")
            continue
        for rec in parse_feed(resp.content):
            post = item_to_posting(ctx.spec.id, rec)
            if post and post.source_job_id not in seen:
                seen.add(post.source_job_id)
                yield post
