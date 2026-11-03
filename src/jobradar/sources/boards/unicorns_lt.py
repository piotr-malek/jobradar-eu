"""Unicorns Lithuania (unicorns.lt) — job board of the Lithuanian startup association.

List: GET https://unicorns.lt/api/more-job?category=<cat>&page=N&search=&city= → {rows: HTML cards, totalResults};
12 cards per page, newest first. Card: title, "Company, City", salary label (mostly N/A), "Remote" tag, date, ``/j/<id>``.
There is no description on the board. ``/j/<id>`` answers 302 to the employer's own ATS page (Greenhouse, Ashby,
Teamtailor, Workday, company sites): for title-relevant, not-yet-stored cards that Location header is read WITHOUT
following it (one request to unicorns.lt, none to the employer) and becomes ``apply_url``, which gives the dedup key.
The same job listed for several cities is one posting (company + title).
robots.txt: ``User-agent: *`` allows everything (it names AI-training crawlers separately; this adapter is a plain job-list reader, not one of them).

Params: categories [...], max_pages (6 per category), max_details (40).
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Iterator

from jobradar.http.client import Blocked, BudgetExceeded
from jobradar.models import Posting
from jobradar.sources.base import FetchContext, adapter, as_list, parse_date

SITE = "https://unicorns.lt"
PAGE = 12
CATEGORIES = ["Data & AI", "Software Engineering", "Product & Project Management", "Marketing & Communications",
              "Sales & Business Development", "Other"]
_CARD = re.compile(r'<div class="card listing.*?(?=<div class="card listing|\Z)', re.S)


def _clean(s: str) -> str:
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def parse_cards(rows: str) -> list[dict]:
    out = []
    for block in _CARD.findall(rows or ""):
        link = re.search(r'href="/j/(\d+)"', block)
        h3 = re.search(r"<h3>(.*?)</h3>", block, re.S)
        comp = re.search(r'class="company">(.*?)</div>', block, re.S)
        if not (link and h3):
            continue
        company_city = _clean(comp.group(1)) if comp else ""
        company, _, city = company_city.rpartition(", ")
        if not company:
            company, city = company_city, ""
        labels = [_clean(x) for x in re.findall(r'<div class="label[^"]*"[^>]*>(.*?)</div>', block, re.S)]
        date = re.search(r'class="time">\s*(\d{4}-\d{2}-\d{2})', block)
        out.append({
            "id": link.group(1), "title": _clean(h3.group(1)), "company": company, "city": city,
            "remote": any(x.lower() == "remote" for x in labels),
            "salary": next((x for x in labels if x not in ("N/A", "New", "Remote") and re.search(r"\d", x)), ""),
            "date": date.group(1) if date else None,
        })
    return out


@adapter("unicorns_lt")
def unicorns_lt(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    max_pages = int(p.get("max_pages", 6))
    max_details = int(p.get("max_details", 40))
    groups: dict[str, dict] = {}  # company|title → card with the list of cities / ids
    stopped = False
    for cat in as_list(p.get("categories", CATEGORIES)):
        for page in range(1, max_pages + 1):
            try:
                resp = ctx.http.get(f"{SITE}/api/more-job", params={"category": cat, "page": page, "search": "", "city": ""})
            except (Blocked, BudgetExceeded) as exc:
                ctx.notes.append(f"list stopped: {exc}")
                stopped = True
                break
            if resp.status_code != 200:
                ctx.notes.append(f"list HTTP {resp.status_code}")
                break
            try:
                data = resp.json()
            except ValueError:
                ctx.notes.append("list not JSON")
                break
            cards = parse_cards(data.get("rows", ""))
            for c in cards:
                key = f"{c['company'].lower()}|{c['title'].lower()}"
                g = groups.get(key)
                if g:
                    if c["city"] and c["city"] not in g["cities"]:
                        g["cities"].append(c["city"])
                    g["remote"] = g["remote"] or c["remote"]
                    continue
                c["cities"] = [c["city"]] if c["city"] else []
                c["category"] = cat
                groups[key] = c
            if len(cards) < PAGE or page * PAGE >= int(data.get("totalResults") or 0):
                break
        if stopped:
            break
    pending = []
    for c in groups.values():
        if not ctx.wants_detail(c["title"]):
            ctx.skipped += 1
            continue
        if ctx.is_known(ctx.spec.id, c["id"]):
            ctx.mark_seen(ctx.spec.id, c["id"])
            continue
        pending.append(c)
    pending.sort(key=lambda c: int(c["id"]), reverse=True)
    if len(pending) > max_details:
        ctx.notes.append(f"{len(pending)} new title-relevant, resolving the newest {max_details}")
    for c in pending[:max_details]:
        apply_url = None
        try:
            r = ctx.http.get(f"{SITE}/j/{c['id']}", follow_redirects=False)
            loc = r.headers.get("location") or ""
            if r.status_code in (301, 302, 303, 307, 308) and loc.startswith("http"):
                apply_url = loc
        except (Blocked, BudgetExceeded) as exc:
            ctx.notes.append(f"redirect stopped: {exc}")
            stopped = True
        cities = c["cities"]
        remote = c["remote"] or "remote" in [x.lower() for x in cities]
        yield Posting(
            source=ctx.spec.id,
            source_job_id=c["id"],
            title=c["title"],
            company=c["company"],
            url=f"{SITE}/j/{c['id']}",
            apply_url=apply_url,
            location_raw=", ".join(cities + ["Lithuania"]) if cities else "Lithuania",
            countries=["LT"],
            cities=[x for x in cities if x.lower() != "remote"],
            workplace="remote" if remote else None,
            posted_at=parse_date(c["date"]),
            tags=[f"category:{c['category'].lower()}"],
            raw={"salary_label": c["salary"]},
        )
        if stopped:
            return
