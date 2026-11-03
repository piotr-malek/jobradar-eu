"""Tecnoempleo (Spain, largest IT board) — search result pages + schema.org JobPosting detail pages.

List:   GET https://www.tecnoempleo.com/ofertas-trabajo/?te=<query>&en_remoto=,<1|3>,&pagina=N
        (``,1,`` = 100% remote, ``,3,`` = hybrid; 30 cards per page, no sort option). Cards carry title, company,
        city, work mode ("100% remoto" / "(Híbrido)"), date and the job url ``/<slug>/<skills>/rf-<hash>``.
        The hybrid slice also returns cards without a work-mode label (on-site / unspecified): those are skipped.
Detail: GET the job url → JobPosting JSON-LD (Spanish text, title, salary, country; ``jobLocationType`` only for
        remote). Fetched only for titles passing ``ctx.wants_detail`` that are not stored yet.
Apply:  account + CV on the board (no company ATS link) → apply_url stays empty, the posting url is used.
robots.txt: ``*`` disallows only admin/ajax/rss paths; search and detail pages are allowed. The RSS path is not used.

Params: queries [...], modes [1, 3], max_pages 3 (per query and slice), max_details 40.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterator

from bs4 import BeautifulSoup

from jobradar.http.client import Blocked, BudgetExceeded
from jobradar.models import Posting
from jobradar.sources.base import FetchContext, adapter, default_terms, as_list, parse_date
from jobradar.sources.generic.sitemap_jsonld import extract_jobposting, jobposting_to_posting

SITE = "https://www.tecnoempleo.com"
PAGE = 30
_ID = re.compile(r"/rf-(\w+)")
_TOTAL = re.compile(r"(\d+)\s+Ofertas", re.I)


def _txt(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def parse_list(html: str) -> tuple[list[dict], int]:
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict] = []
    for card in soup.select("div.p-3.border.rounded.mb-3"):
        a = card.select_one("h3 a[href*='/rf-']")
        if not a:
            continue
        href = a["href"].split("?")[0]
        m = _ID.search(href)
        if not m:
            continue
        right = card.select_one("div.text-right")
        rtext = _txt(right)
        mode = "remote" if "100% remoto" in rtext.lower() else "hybrid" if "híbrido" in rtext.lower() else None
        city = _txt(right.find("b")) if right and right.find("b") else ""
        date = re.search(r"(\d{2})/(\d{2})/(\d{4})", rtext)
        company = card.select_one("a[href*='/re-']")
        rows.append({
            "id": m.group(1), "url": href, "title": (a.get("title") or _txt(a)).strip(),
            "company": _txt(company), "city": city, "mode": mode,
            "date": f"{date.group(3)}-{date.group(2)}-{date.group(1)}" if date else None,
        })
    h1 = soup.find("h1")
    total = _TOTAL.search(_txt(h1)) if h1 else None
    return rows, int(total.group(1)) if total else 0


def parse_detail(row: dict, html: str, source: str) -> Posting | None:
    jp = extract_jobposting(html)
    if not jp:
        return None
    post = jobposting_to_posting(jp, source=source, source_job_id=row["id"], url=row["url"],
                                 default_company=row["company"])
    post.title = post.title or row["title"]
    if row["mode"]:
        post.workplace = row["mode"]
    if not post.cities and row["city"]:
        post.cities = [row["city"]]
    if not post.countries:
        post.countries = ["ES"]
    if not post.location_raw:
        post.location_raw = ", ".join(x for x in (row["city"], "España") if x)
    post.posted_at = parse_date(jp.get("datePosted")) or parse_date(row["date"])
    valid = parse_date(jp.get("validThrough"))
    if valid and valid < datetime.now(timezone.utc):
        return None
    return post


@adapter("tecnoempleo")
def tecnoempleo(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    max_pages = int(p.get("max_pages", 3))
    max_details = int(p.get("max_details", 40))
    modes = [str(m) for m in as_list(p.get("modes", [1, 3]))]
    seen: set[str] = set()
    pending: list[dict] = []
    stopped = False
    for q in as_list(p.get("queries") or default_terms(11, ["es"])):
        for mode in modes:
            for page in range(1, max_pages + 1):
                try:
                    resp = ctx.http.get(f"{SITE}/ofertas-trabajo/",
                                        params={"te": q, "en_remoto": f",{mode},", "pagina": page})
                except (Blocked, BudgetExceeded) as exc:
                    ctx.notes.append(f"list stopped: {exc}")
                    stopped = True
                    break
                if resp.status_code != 200:
                    ctx.notes.append(f"list HTTP {resp.status_code}")
                    break
                rows, total = parse_list(resp.text)
                for row in rows:
                    if row["id"] in seen:
                        continue
                    seen.add(row["id"])
                    if mode == "3" and row["mode"] != "hybrid":
                        ctx.skipped += 1  # unlabelled / on-site card in the hybrid slice
                    elif not ctx.wants_detail(row["title"]):
                        ctx.skipped += 1
                    elif ctx.is_known(ctx.spec.id, row["id"]):
                        ctx.mark_seen(ctx.spec.id, row["id"])
                    else:
                        pending.append(row)
                if len(rows) < PAGE or page * PAGE >= total:
                    break
            if stopped:
                break
        if stopped:
            break
    if len(pending) > max_details:
        ctx.notes.append(f"{len(pending) - max_details} new relevant jobs beyond max_details")
    for row in pending[:max_details]:
        try:
            resp = ctx.http.get(row["url"])
        except (Blocked, BudgetExceeded) as exc:
            ctx.notes.append(f"detail stopped: {exc}")
            break
        if resp.status_code != 200:
            continue
        post = parse_detail(row, resp.text, ctx.spec.id)
        if post:
            yield post
