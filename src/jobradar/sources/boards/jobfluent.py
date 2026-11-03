"""Jobfluent (Spain; English-friendly startup/tech jobs) — city sitemaps + server-rendered detail pages.

Sitemaps: https://www.jobfluent.com/sitemaps/jobs-{remote,barcelona,madrid,valencia}/offers-list.xml
          (a remote job also appears in its city sitemap: deduped by the 6-hex id at the end of the url).
Detail:   GET /jobs/<title>-<city>-<id> → HTML with microdata (title, city, company, category, industry, skills),
          a "Workplace" row (Remote / Onsite / Hybrid), "Hours", and the description in ``itemprop=description``
          (present in the HTML even though the page nags for a LinkedIn login). No JSON-LD, no posting date.
Apply:    needs a LinkedIn login on the board → apply_url stays empty (posting url is used).
robots.txt: ``/jobs/*`` allowed for ``*``; ``/api/*`` and ``/*/external`` are disallowed and never used.

Params: sitemaps [remote, barcelona, madrid, valencia], max_details 40.
"""

from __future__ import annotations

import re
from typing import Iterator

from bs4 import BeautifulSoup

from jobradar.http.client import Blocked, BudgetExceeded
from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, as_list
from jobradar.sources.generic.sitemap_jsonld import decode_body, parse_sitemap

SITE = "https://www.jobfluent.com"
DEFAULT_SITEMAPS = ["remote", "barcelona", "madrid", "valencia"]
_URL = re.compile(r"/jobs/(?P<slug>[^/?#]+)-(?P<id>[0-9a-f]{6})$")
_CITIES = ("barcelona", "madrid", "valencia", "sevilla", "malaga", "bilbao", "zaragoza", "remote")
_MODE = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}


def _txt(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def slug_title(slug: str) -> str:
    for c in _CITIES:
        if slug.endswith("-" + c):
            slug = slug[: -len(c) - 1]
            break
    return slug.replace("-", " ")


def _meta(soup: BeautifulSoup, prop: str) -> str:
    el = soup.select_one(f'[itemprop="{prop}"]')
    return (el.get("content") if el and el.name == "meta" else _txt(el)) or ""


def parse_detail(html: str, url: str, source: str, job_id: str) -> Posting | None:
    soup = BeautifulSoup(html, "lxml")
    title = _txt(soup.select_one('h1 [itemprop="title"]'))
    body = soup.select_one('div[itemprop="description"]')
    if not title or not body:
        return None
    city = _txt(soup.select_one('[itemprop="addressLocality"]'))
    country = _meta(soup, "addressCountry") or "ES"
    company = _txt(soup.select_one('[itemprop="hiringOrganization"] [itemprop="name"]'))
    workplace = None
    for row in soup.select("div.row.m-b-20"):
        label = _txt(row.find("b"))
        if label == "Workplace":
            workplace = _MODE.get(_txt(row.select_one("b.text-pale")).lower())
    hours = _txt(soup.select_one('[itemprop="employmentType"]'))
    skills = [s.strip() for s in _meta(soup, "skills").split(",") if s.strip()]
    tags = skills + [t for t in (_meta(soup, "occupationalCategory"),) if t]
    industry = _txt(soup.select_one('[itemprop="industry"]'))
    return Posting(
        source=source,
        source_job_id=job_id,
        title=title,
        company=company,
        url=url,
        apply_url=None,
        location_raw=", ".join(x for x in (city, "Spain" if country == "ES" else country) if x),
        countries=[country] if len(country) == 2 else ["ES"],
        cities=[city] if city else [],
        workplace=workplace,
        description=html_to_text(str(body)),
        employment_types=[hours] if hours else [],
        tags=list(dict.fromkeys(tags)),
        raw={"industry": industry},
    )


@adapter("jobfluent")
def jobfluent(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    max_details = int(p.get("max_details", 40))
    pending: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in as_list(p.get("sitemaps", DEFAULT_SITEMAPS)):
        try:
            resp = ctx.http.get(f"{SITE}/sitemaps/jobs-{name}/offers-list.xml")
        except (Blocked, BudgetExceeded) as exc:
            ctx.notes.append(f"sitemap {name} stopped: {exc}")
            continue
        if resp.status_code != 200:
            ctx.notes.append(f"sitemap {name}: HTTP {resp.status_code}")
            continue
        _, entries = parse_sitemap(decode_body(resp.content))
        for e in entries:
            m = _URL.search(e.loc.split("?")[0])
            if not m or m.group("id") in seen:
                continue
            jid = m.group("id")
            seen.add(jid)
            if ctx.is_known(ctx.spec.id, jid):
                ctx.mark_seen(ctx.spec.id, jid)
            elif not ctx.wants_detail(slug_title(m.group("slug"))):
                ctx.skipped += 1
            else:
                pending.append((e.loc.split("?")[0], jid))
    if len(pending) > max_details:
        ctx.notes.append(f"{len(pending) - max_details} new relevant jobs beyond max_details")
    for url, jid in pending[:max_details]:
        try:
            resp = ctx.http.get(url)
        except (Blocked, BudgetExceeded) as exc:
            ctx.notes.append(f"detail stopped: {exc}")
            break
        if resp.status_code != 200:
            continue
        post = parse_detail(resp.text, url, ctx.spec.id, jid)
        if post:
            yield post
