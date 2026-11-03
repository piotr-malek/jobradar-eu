"""jobs.ch (Switzerland) — server-rendered search pages + schema.org JobPosting JSON-LD.

robots.txt disallows ``/api/`` (the SPA's search API), so we only use what it serves to crawlers:

1. GET https://www.jobs.ch/en/vacancies/?term=<q>&page=N → JSON-LD ``ItemList`` of 20 JobPosting stubs
   (title, identifier uuid, datePosted, employmentType, hiringOrganization, addressCountry, url).
2. GET https://www.jobs.ch/en/vacancies/detail/<uuid>/ (allowed; only ``/detail/*/*/*`` is
   disallowed) → full JobPosting (HTML description, address, baseSalary) for titles passing
   ``ctx.wants_detail`` that are not stored yet (``max_details`` per run).

Params: queries (default: the role packs' search terms), max_pages 2, max_details 20, lang en.
jobup.ch (the French-speaking JobCloud site) serves the same markup: ``site: https://www.jobup.ch``,
``path: en/jobs`` (detail ``/en/jobs/detail/<uuid>/``).
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator
from urllib.parse import quote

from jobradar.http.client import Blocked, BudgetExceeded
from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, default_terms, as_list, first, parse_date
from jobradar.sources.generic.rss import merge_jobposting
from jobradar.sources.generic.sitemap_jsonld import country_iso2

SITE = "https://www.jobs.ch"
_LD = re.compile(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I)
_UUID = re.compile(r"/detail/([0-9a-f-]{36})/?")


def list_jobpostings(page: str) -> list[dict[str, Any]]:
    """JobPosting dicts from the search page's JSON-LD ItemList(s)."""
    out: list[dict[str, Any]] = []
    for m in _LD.finditer(page):
        try:
            data = json.loads(m.group(1), strict=False)
        except ValueError:
            continue
        for block in as_list(data):
            if not isinstance(block, dict):
                continue
            for el in as_list(block.get("itemListElement")):
                item = el.get("item") if isinstance(el, dict) else None
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    out.append(item)
    return out


def stub_to_posting(source: str, jp: dict[str, Any], base: str = f"{SITE}/en/vacancies") -> Posting | None:
    url = jp.get("url") or ""
    ident = jp.get("identifier") or {}
    jid = first(ident.get("value") if isinstance(ident, dict) else None, (_UUID.search(url) or [None, None])[1])
    if not jid or not jp.get("title"):
        return None
    org = jp.get("hiringOrganization") or {}
    addr = ((jp.get("jobLocation") or {}).get("address") or {}) if isinstance(jp.get("jobLocation"), dict) else {}
    iso = country_iso2(addr.get("addressCountry")) if addr else None
    return Posting(
        source=source,
        source_job_id=str(jid),
        title=str(jp["title"]).strip(),
        company=(org.get("name") or "").strip() if isinstance(org, dict) else "",
        url=url or f"{base}/detail/{jid}/",
        location_raw=", ".join(x for x in (addr.get("addressLocality"), addr.get("addressRegion"), iso) if x),
        countries=[iso] if iso else [],
        description="",  # list stub text is boilerplate ("We are looking for a … to join our team.")
        posted_at=parse_date(jp.get("datePosted")),
        employment_types=as_list(jp.get("employmentType")),
        raw={"stub": html_to_text(jp.get("description"))[:200]},
    )


@adapter("jobsch")
def jobsch(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    lang = p.get("lang", "en")
    path = p.get("path") or {"en": "en/vacancies", "de": "de/stellenangebote", "fr": "fr/offres-emplois"}.get(lang, "en/vacancies")
    base = f"{p.get('site', SITE).rstrip('/')}/{path}"
    max_details = int(p.get("max_details", 20))
    seen: set[str] = set()
    to_detail: list[Posting] = []
    stop = False
    for query in as_list(p.get("queries") or default_terms(4)):
        for page in range(1, int(p.get("max_pages", 2)) + 1):
            url = f"{base}/?term={quote(query)}" + (f"&page={page}" if page > 1 else "")
            try:
                resp = ctx.http.get(url)
            except (Blocked, BudgetExceeded) as exc:
                ctx.notes.append(f"search stopped: {exc}")
                stop = True
                break
            if resp.status_code != 200:
                ctx.notes.append(f"{url}: HTTP {resp.status_code}")
                break
            stubs = list_jobpostings(resp.text)
            if not stubs:
                ctx.notes.append(f"no JSON-LD ItemList on {url}")
                break
            for jp in stubs:
                post = stub_to_posting(ctx.spec.id, jp, base)
                if not post or post.source_job_id in seen:
                    continue
                seen.add(post.source_job_id)
                if not ctx.wants_detail(post.title):
                    yield post
                elif not ctx.is_known(ctx.spec.id, post.source_job_id):
                    to_detail.append(post)
            if len(stubs) < 20:
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
                if resp.status_code == 200 and not merge_jobposting(post, resp.text):
                    ctx.notes.append(f"no JobPosting on {post.url}")
            except (Blocked, BudgetExceeded) as exc:
                blocked = True
                ctx.notes.append(f"detail stopped: {exc}")
        yield post
