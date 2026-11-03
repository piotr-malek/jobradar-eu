"""CV-Online platform — cv.ee (Estonia), cv.lv (Latvia) and cvonline.lt (Lithuania): one adapter, one entry per site.

Sitemap: GET https://<host>/jobs-sitemap.xml → ``/<lang>/vacancy/<id>/<employer>/<title-slug>`` (1.2k–1.9k URLs per site,
robots allow everything). ``lastmod`` is useless (every row is re-stamped), so the numeric id decides what is new.
The sitemap carries the title only as a slug, so the matcher's cheap title gate runs on the slug and only
title-relevant, not-yet-stored vacancies are fetched.
Detail: the ``/en/`` URL (same page in English chrome) is ~700 KB; its ``__NEXT_DATA__`` holds the whole vacancy in
``props.pageProps.initialReduxState.publicVacancies["<id>"]``: position, employerName, firstPublishDate,
highlights (remoteWork / remoteWorkType HYBRID|ON_SITE|REMOTE, salaryFrom/To, ratePer, location ids resolved with
``pageProps.locations``), languages[].iso, settings.applyingUrl (the employer's ATS in most cases) and
details.standardDetails[].content (HTML description; empty for
IFRAME vacancies whose text lives on the employer's ATS, e.g. SEB on Lever → ``details.urlDetails``). JSON-LD on these pages has an empty description, so it is not used.

Language mapping: required languages other than en/pl are appended to the description as "German required."
(the same wording remoterocketship and ictjob use), so ``language_requirements`` sees them.

Params: host (www.cv.ee), country (EE), max_details (30), lang (en).
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

from jobradar.http.client import Blocked, BudgetExceeded
from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, parse_date, to_float
from jobradar.sources.generic.sitemap_jsonld import decode_body, parse_sitemap

_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_VACANCY = re.compile(r"/(?P<lang>[a-z]{2})/vacancy/(?P<id>\d+)/(?P<employer>[^/?#]+)/(?P<slug>[^/?#]+)")
_LANG_NAMES = {
    "et": "Estonian", "lv": "Latvian", "lt": "Lithuanian", "ru": "Russian", "de": "German", "fr": "French",
    "nl": "Dutch", "es": "Spanish", "it": "Italian", "pt": "Portuguese", "sv": "Swedish", "da": "Danish",
    "no": "Norwegian", "fi": "Finnish", "cs": "Czech", "hu": "Hungarian", "ro": "Romanian", "uk": "Ukrainian",
}
_WORKPLACE = {"HYBRID": "hybrid", "ON_SITE": "onsite", "REMOTE": "remote"}
_PERIOD = {"MONTHLY": "month", "HOURLY": "hour", "YEARLY": "year", "ANNUALLY": "year", "DAILY": "day"}


def parse_vacancy_html(html: str, vacancy_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(vacancy dict, pageProps) from a detail page, or None."""
    m = _NEXT.search(html)
    if not m:
        return None
    try:
        pp = json.loads(m.group(1))["props"]["pageProps"]
        pv = pp["initialReduxState"]["publicVacancies"]
    except (ValueError, KeyError, TypeError):
        return None
    v = pv.get(str(vacancy_id))
    if not isinstance(v, dict) or not v.get("position"):
        return None
    return v, pp


def _place(v: dict[str, Any], pp: dict[str, Any], default_country: str) -> tuple[str, list[str], list[str]]:
    loc = ((v.get("highlights") or {}).get("location")) or {}
    places = pp.get("locations") or {}
    town = next((t.get("name") for t in places.get("towns") or [] if t.get("id") == loc.get("townId")), None)
    county = ((places.get("counties") or {}).get(str(loc.get("countyId"))) or {}).get("name")
    country = ((places.get("countries") or {}).get(str(loc.get("countryId"))) or {})
    iso = country.get("iso") or default_country
    name = country.get("name") or ""
    raw = ", ".join(x for x in (town or county, name) if x)
    return raw or default_country, [iso] if iso else [], [town] if town else []


def to_posting(source: str, v: dict[str, Any], pp: dict[str, Any], url: str, default_country: str) -> Posting:
    hl = v.get("highlights") or {}
    settings = v.get("settings") or {}
    parts = [html_to_text(d.get("content")) for d in ((v.get("details") or {}).get("standardDetails") or [])]
    iso = [str(x.get("iso") or "").lower() for x in v.get("languages") or []]
    need = [_LANG_NAMES[c] for c in iso if c in _LANG_NAMES]
    if need:
        parts.append("Languages: " + " ".join(f"{n} required." for n in need))
    raw_mode = hl.get("remoteWorkType")
    workplace = _WORKPLACE.get(raw_mode) or ("remote" if hl.get("remoteWork") else None)
    location_raw, countries, cities = _place(v, pp, default_country)
    apply_url = (settings.get("applyingUrl") or (v.get("details") or {}).get("urlDetails") or "").strip() or None
    sal_from, sal_to = to_float(hl.get("salaryFrom")), to_float(hl.get("salaryTo"))
    return Posting(
        source=source,
        source_job_id=str(v["id"]),
        title=str(v["position"]).strip(),
        company=str(v.get("employerName") or "").strip(),
        url=url,
        apply_url=apply_url,
        location_raw=location_raw,
        countries=countries,
        cities=cities,
        workplace=workplace,
        description="\n\n".join(p for p in parts if p).strip(),
        posted_at=parse_date(v.get("firstPublishDate") or v.get("dateCreated")),
        employment_types=[str(w).lower() for w in hl.get("workTimes") or []],
        salary_min=sal_from,
        salary_max=sal_to,
        salary_currency="EUR" if (sal_from or sal_to) else None,
        salary_period=_PERIOD.get(str(hl.get("ratePer") or "").upper()) if (sal_from or sal_to) else None,
        tags=[f"lang:{c}" for c in iso if c] + [f"category:{c.lower()}" for c in settings.get("categories") or []],
        raw={"language": v.get("languageIso"), "remote_type": raw_mode},
    )


@adapter("cvonline")
def cvonline(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    host = p.get("host", "www.cv.ee")
    country = p.get("country", "EE")
    lang = p.get("lang", "en")
    max_details = int(p.get("max_details", 30))
    try:
        resp = ctx.http.get(f"https://{host}/jobs-sitemap.xml")
    except (Blocked, BudgetExceeded) as exc:
        ctx.notes.append(f"sitemap stopped: {exc}")
        return
    if resp.status_code != 200:
        ctx.notes.append(f"sitemap HTTP {resp.status_code}")
        return
    _, entries = parse_sitemap(decode_body(resp.content))
    pending: list[tuple[int, str]] = []
    seen: set[str] = set()
    for e in entries:
        m = _VACANCY.search(e.loc)
        if not m or m.group("id") in seen:
            continue
        vid = m.group("id")
        seen.add(vid)
        if not ctx.wants_detail(m.group("slug").replace("-", " ")):
            ctx.skipped += 1
            continue
        if ctx.is_known(ctx.spec.id, vid):
            ctx.mark_seen(ctx.spec.id, vid)
            continue
        pending.append((int(vid), e.loc))
    pending.sort(reverse=True)  # newest ids first when the detail budget is smaller than the backlog
    if len(pending) > max_details:
        ctx.notes.append(f"{len(pending)} new title-relevant, fetching the newest {max_details}")
    for vid, loc in pending[:max_details]:
        url = _VACANCY.sub(lambda m: f"/{lang}/vacancy/{m.group('id')}/{m.group('employer')}/{m.group('slug')}", loc)
        try:
            resp = ctx.http.get(url)
        except (Blocked, BudgetExceeded) as exc:
            ctx.notes.append(f"detail stopped: {exc}")
            return
        if resp.status_code != 200:
            ctx.notes.append(f"detail HTTP {resp.status_code}")
            continue
        parsed = parse_vacancy_html(resp.text, str(vid))
        if not parsed:
            ctx.notes.append("detail without vacancy data")
            continue
        yield to_posting(ctx.spec.id, parsed[0], parsed[1], url, country)
