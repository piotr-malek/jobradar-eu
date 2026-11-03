"""SAP SuccessFactors Career Site Builder (UN agencies: ITU, UNESCO, ILO, …): sitemap → detail pages.

Job pages (``/job/{City}-{Title}/{id}/``) carry schema.org *microdata* (``itemprop`` meta tags), not JSON-LD, so
the generic ``sitemap_jsonld`` adapter cannot read them. Fields are taken from the microdata (ITU, UNESCO) or the
"Grade: P3 / Location: …" header block in the description (ILO, UNESCO, ITU). Purpose track: ``grade:<value>``
tags are added, internships are dropped from the sitemap slug, the matcher filters on grade.

Params: host (required), org (company name; default: page title suffix), max_details (25),
title_gate (true: ctx.wants_detail on the slug text), skip_known (true).
"""

from __future__ import annotations

import html as html_lib
import re
from datetime import datetime, timezone
from typing import Iterator
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from jobradar.http.client import Blocked
from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, parse_date
from jobradar.sources.boards.national_apis import country_iso
from jobradar.sources.boards.un_grades import grade_tag, is_non_job
from jobradar.sources.generic.sitemap_jsonld import collect_sitemap_urls

_ID = re.compile(r"/job/(?P<slug>[^/]+)/(?P<id>\d+)/?")
_TITLE_SUFFIX = re.compile(r"\s+(?:Job Details|Détails du poste|Detalles del puesto)\s*\|.*$", re.I)
_REMOTE = re.compile(r"\b(home[\s-]?based|remote|telecommut\w*)\b", re.I)
_GRADE_FIELD = re.compile(r"\bGrade\s*:\s*([A-Za-z]{1,4}[\s-]?[0-9A-D]{1,2})\b")
_LEVEL_FIELD = re.compile(r"\bLevel\s*:\s*(Level\s*\d[^:]{0,30}?)(?=\s+[A-Z][A-Za-z ]{2,25}\s*:|$)")
_STOP = (r"(?=\s+(?:Contract|Type of|Grade|Position|Parent|Job|Duration|Recruitment|Hiring|Application|Department|"
         r"Organi[sz]ation|Vacancy|Publication|Level|Category|Duty|Work|Location|Sector|Country)\b|$)")
_CONTRACT = re.compile(r"\b(?:Type of contract|Contract [Tt]ype)\s*:\s*(.{1,40}?)" + _STOP)
_LOCATION = re.compile(r"\b(?:Duty [Ss]tation|Location)\s*:\s*(.{1,60}?)" + _STOP)
_PUBLISHED = re.compile(r"Publication date\s*:\s*(\d{1,2} [A-Za-z]+ \d{4})")


def slug_title(slug: str) -> str:
    return re.sub(r"[-_+]+", " ", html_lib.unescape(unquote(slug))).strip()


def _meta(soup: BeautifulSoup, prop: str) -> str:
    m = soup.find("meta", attrs={"itemprop": prop})
    return (m.get("content") or "").strip() if m else ""


def _posted(value: str) -> datetime | None:
    try:  # "Fri Oct 02 02:00:00 UTC 2026"
        return datetime.strptime(value, "%a %b %d %H:%M:%S UTC %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return parse_date(value)


def _dmy(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%d %B %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_job(page: str, *, host_org: str | None = None) -> dict:
    soup = BeautifulSoup(page, "lxml")
    title = html_lib.unescape(_TITLE_SUFFIX.sub("", soup.title.get_text(strip=True) if soup.title else "")).strip()
    box = soup.select_one("div.jobDisplay") or soup
    body = box.get_text(" ", strip=True)
    street = _meta(soup, "streetAddress")
    parts = [x for x in (_meta(soup, "addressLocality"), _meta(soup, "addressRegion"), _meta(soup, "addressCountry")) if x]
    loc_m = _LOCATION.search(body)
    location = street or ", ".join(parts) or (loc_m.group(1).strip() if loc_m else "")
    grade_m = _GRADE_FIELD.search(body)
    contract_m = _CONTRACT.search(body)
    level_m = _LEVEL_FIELD.search(body)
    grade = grade_tag(grade_m.group(1)) if grade_m else None
    contract = contract_m.group(1).strip() if contract_m else ""
    if not grade:
        grade = grade_tag(contract) or (grade_tag(title) if re.search(r"consultan|roster", title, re.I) else None)
    org = _meta(soup, "hiringOrganization") or host_org or ""
    published = _meta(soup, "datePosted")
    pub_m = _PUBLISHED.search(body)
    return {
        "title": title, "org": org, "location": location, "grade": grade, "contract": contract,
        "level": level_m.group(1).strip() if level_m else None,
        "posted": _posted(published) if published else (_dmy(pub_m.group(1)) if pub_m else None),
        "valid_through": _posted(_meta(soup, "validThrough")) if _meta(soup, "validThrough") else None,
        "description": html_to_text(str(box)),
    }


@adapter("sf_csb")
def sf_csb(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    host = p["host"]
    max_details = int(p.get("max_details", 25))
    title_gate = bool(p.get("title_gate", True))
    ctx.spec.params.setdefault("sitemaps", [f"https://{host}/sitemap.xml"])
    entries, _ = collect_sitemap_urls(ctx)
    base = {"entries": len(entries), "known": 0, "fetched": 0}
    queue: list[tuple[str, str, str]] = []
    for e in entries:
        m = _ID.search(urlsplit(e.loc).path)
        if not m:
            continue
        jid, slug = m.group("id"), slug_title(m.group("slug"))
        if is_non_job(slug):
            ctx.skipped += 1
            continue
        if p.get("skip_known", True) and ctx.is_known(ctx.spec.id, jid):
            ctx.mark_seen(ctx.spec.id, jid)
            base["known"] += 1
            continue
        if title_gate and not ctx.wants_detail(slug):
            ctx.skipped += 1
            continue
        queue.append((jid, e.loc, slug))
    for jid, url, slug in queue:
        if base["fetched"] >= max_details or ctx.http.remaining <= 0:
            break
        try:
            resp = ctx.http.get(url)
        except Blocked as exc:
            ctx.notes.append(f"{jid}: {exc.reason}")
            continue
        base["fetched"] += 1
        if resp.status_code != 200:
            ctx.notes.append(f"{jid}: HTTP {resp.status_code}")
            continue
        d = parse_job(resp.text, host_org=p.get("org"))
        title = d["title"] or slug
        if is_non_job(title) or is_non_job(d["contract"]):
            ctx.skipped += 1
            continue
        loc = d["location"] or slug.split(" ")[0]
        iso = [c for c in (country_iso(x.strip()) for x in loc.split(",")) if c]
        yield Posting(
            source=ctx.spec.id, source_job_id=jid, title=title, company=p.get("org") or d["org"], url=url,
            location_raw=loc, countries=list(dict.fromkeys(iso)),
            workplace="remote" if _REMOTE.search(loc) or re.search(r"home[\s-]?based", slug, re.I) else None,
            description=d["description"], posted_at=d["posted"],
            employment_types=[d["contract"]] if d["contract"] else [],
            tags=[d["grade"]] if d["grade"] else [],
            raw={"validThrough": d["valid_through"].isoformat() if d["valid_through"] else None,
                 "level": d["level"], "host": host},
        )
    ctx.notes.append(f"sitemap_urls={base['entries']} known={base['known']} queued={len(queue)} fetched={base['fetched']}")
