"""National public-employment-service APIs (DE, SE, NO) and the EU-wide EURES portal.

All four are official, key-less (or public-token) JSON APIs run by public bodies;
they aggregate a large share of each country's vacancies, so they are high-value
but mostly local-language and mostly on-site/hybrid. Matching happens later.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx

from jobradar.http.client import Blocked
from jobradar.models import Posting
from jobradar.normalize.text import fold, html_to_text
from jobradar.sources.base import FetchContext, adapter, default_terms, as_list, first, parse_date, to_float

# --------------------------------------------------------------- countries
# Country names as they appear in these APIs (German enum names from the BA, native
# names from NAV/JobTech) → ISO-3166 alpha-2. Folded (lowercase, no diacritics).
_COUNTRY_NAMES = {
    "deutschland": "DE", "germany": "DE", "oesterreich": "AT", "osterreich": "AT", "austria": "AT",
    "schweiz": "CH", "switzerland": "CH", "liechtenstein": "LI", "luxemburg": "LU", "luxembourg": "LU",
    "niederlande": "NL", "netherlands": "NL", "belgien": "BE", "belgium": "BE", "frankreich": "FR",
    "france": "FR", "italien": "IT", "italy": "IT", "spanien": "ES", "spain": "ES", "portugal": "PT",
    "irland": "IE", "ireland": "IE", "polen": "PL", "poland": "PL", "tschechien": "CZ",
    "tschechische republik": "CZ", "czechia": "CZ", "slowakei": "SK", "ungarn": "HU", "rumaenien": "RO",
    "rumanien": "RO", "bulgarien": "BG", "kroatien": "HR", "slowenien": "SI", "griechenland": "GR",
    "zypern": "CY", "malta": "MT", "daenemark": "DK", "danemark": "DK", "danmark": "DK", "denmark": "DK",
    "schweden": "SE", "sverige": "SE", "sweden": "SE", "norwegen": "NO", "norge": "NO", "noreg": "NO",
    "norway": "NO", "finnland": "FI", "suomi": "FI", "finland": "FI", "estland": "EE", "lettland": "LV",
    "litauen": "LT", "island": "IS", "iceland": "IS", "grossbritannien": "GB", "vereinigtes koenigreich": "GB",
    "vereinigtes konigreich": "GB", "united kingdom": "GB", "usa": "US", "vereinigte staaten": "US",
    "tuerkei": "TR", "turkei": "TR", "ukraine": "UA",
}


def country_iso(name: Any) -> str | None:
    """Structured country name/code → ISO-2 (None when unknown)."""
    if not name:
        return None
    s = str(name).strip()
    if len(s) == 2 and s.isalpha():
        return s.upper()
    key = fold(s).replace("_", " ").strip()
    return _COUNTRY_NAMES.get(key)


def local_date(value: Any, tz: str) -> datetime | None:
    """parse_date for APIs that send naive *local* timestamps (treat them as ``tz``)."""
    if isinstance(value, str) and value and value[-1] != "Z" and "+" not in value[10:] and "T" in value:
        try:
            return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
        except ValueError:
            pass
    return parse_date(value)


def _qs(params: dict[str, Any]) -> str:
    """Deterministic query string (so recorded fixtures replay by exact URL)."""
    return urlencode({k: v for k, v in params.items() if v not in (None, "")}, quote_via=quote)


# ============================================================ Bundesagentur (DE)
BA_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
BA_HEADERS = {"X-API-Key": "jobboerse-jobsuche"}


def _ba_location(locs: list[dict]) -> tuple[str, list[str], list[str]]:
    raw, countries, cities = [], [], []
    for loc in locs:
        a = loc.get("adresse") or loc  # search: {adresse:{...}}, details: flat
        city = (a.get("ort") or "").split(",")[0].strip()
        iso = country_iso(a.get("land"))
        parts = [x for x in (city, (a.get("region") or "").replace("_", " ").title() or None, a.get("land")) if x]
        if parts:
            raw.append(", ".join(str(p) for p in parts))
        if iso and iso not in countries:
            countries.append(iso)
        if city and city not in cities:
            cities.append(city)
    return " | ".join(dict.fromkeys(raw)), countries, cities


def _ba_workplace(j: dict) -> str | None:
    pct = to_float(j.get("homeofficeprozent"))
    if pct is not None and pct >= 100:
        return "remote"
    if j.get("homeofficemoeglich"):
        return "hybrid"  # BA: "Homeoffice möglich" (by arrangement or < 100 %)
    return None


@adapter("arbeitsagentur")
def arbeitsagentur(ctx: FetchContext) -> Iterator[Posting]:
    """Bundesagentur für Arbeit Jobsuche (official, public client id).

    Search: GET /pc/v6/jobs (was, size<=100, page, veroeffentlichtseit, homeoffice).
    ``homeoffice=prozentual_<min%>;nv_true`` is what the BA web UI sends for its
    "Homeoffice möglich" filter (``nv_true`` = also jobs with home office "by
    arrangement" and no stated percentage). Details: GET /pc/v4/jobdetails/{b64(refnr)}.
    """
    p = ctx.params
    searches = p.get("searches") or default_terms(5, ["de"])
    size = min(int(p.get("size", 50)), 100)
    max_pages = int(p.get("max_pages", 2))
    days = p.get("published_within_days", 7)
    homeoffice = p.get("homeoffice", "prozentual_0;nv_true")  # null → no home-office filter
    fetch_details = bool(p.get("fetch_details", True))
    max_details = int(p.get("max_details", 40))
    reserve = int(p.get("reserve_requests", 2))
    seen: set[str] = set()
    details_done = 0
    for was in searches:
        for page in range(1, max_pages + 1):
            q = {"was": was, "size": size, "page": page, "angebotsart": 1,
                 "veroeffentlichtseit": days, "homeoffice": homeoffice}
            data = ctx.http.get_json(f"{BA_BASE}/pc/v6/jobs?{_qs(q)}", headers=BA_HEADERS)
            rows = data.get("ergebnisliste") or data.get("stellenangebote") or []
            for j in rows:
                refnr = j.get("referenznummer") or j.get("refnr")
                if not refnr or refnr in seen:
                    continue
                seen.add(refnr)
                title = j.get("stellenangebotsTitel") or j.get("titel") or j.get("hauptberuf") or ""
                if ctx.wants_detail(title) and ctx.is_known(ctx.spec.id, refnr):
                    ctx.mark_seen(ctx.spec.id, refnr)
                    continue  # stored with its details already; a list-level re-yield would churn versions
                detail: dict[str, Any] = {}
                if (fetch_details and details_done < max_details and ctx.wants_detail(title)
                        and ctx.http.remaining > reserve):
                    code = base64.b64encode(refnr.encode()).decode()
                    try:
                        resp = ctx.http.get(f"{BA_BASE}/pc/v4/jobdetails/{code}", headers=BA_HEADERS)
                        details_done += 1
                        if resp.status_code == 200:
                            detail = resp.json()
                        else:
                            ctx.notes.append(f"jobdetails {refnr}: HTTP {resp.status_code}")
                    except Blocked as exc:
                        ctx.notes.append(f"jobdetails blocked ({exc.status}); details disabled")
                        fetch_details = False
                loc_raw, countries, cities = _ba_location(detail.get("arbeitsorte") or j.get("stellenlokationen") or [])
                ext = j.get("externeURL") or detail.get("externeURL")
                sal_min = to_float(first(j.get("gehaltsspanneVon"), detail.get("gehaltsspanneVon")))
                sal_max = to_float(first(j.get("gehaltsspanneBis"), detail.get("gehaltsspanneBis")))
                period = {"JAHRESGEHALT": "year", "MONATSGEHALT": "month", "STUNDENLOHN": "hour"}.get(
                    str(j.get("verguetungsangabe")))
                merged = {**j, **{k: v for k, v in detail.items() if v not in (None, "")}}
                yield Posting(
                    source=ctx.spec.id,
                    source_job_id=refnr,
                    title=title,
                    company=j.get("firma") or detail.get("arbeitgeber") or "",
                    url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}",
                    apply_url=ext or None,
                    location_raw=loc_raw,
                    countries=countries,
                    cities=cities,
                    workplace=_ba_workplace(merged),
                    description=html_to_text(first(detail.get("stellenangebotsBeschreibung"),
                                                   detail.get("stellenbeschreibung"))),
                    posted_at=parse_date(first(j.get("datumErsteVeroeffentlichung"),
                                               (j.get("veroeffentlichungszeitraum") or {}).get("von"),
                                               j.get("aenderungsdatum"))),
                    employment_types=[x for x in (
                        "full_time" if j.get("arbeitszeitVollzeit") else None,
                        "part_time" if any(j.get(k) for k in ("arbeitszeitTeilzeitVormittag",
                                                             "arbeitszeitTeilzeitNachmittag",
                                                             "arbeitszeitTeilzeitAbend",
                                                             "arbeitszeitTeilzeitFlexibel")) else None,
                        "temporary" if j.get("vertragsdauer") == "BEFRISTET" else None,
                    ) if x],
                    salary_min=sal_min,
                    salary_max=sal_max,
                    salary_currency="EUR" if (sal_min or sal_max) else None,
                    salary_period=period if (sal_min or sal_max) else None,
                    tags=[t for t in as_list(j.get("alleBerufe")) if t],
                    raw={k: merged.get(k) for k in ("homeofficemoeglich", "homeofficetyp", "homeofficeprozent",
                                                    "hauptberuf", "externeURL", "chiffrenummer", "aenderungsdatum")
                         if merged.get(k) not in (None, "")},
                )
            if len(rows) < size or page * size >= int(data.get("maxErgebnisse") or 0):
                break


# ================================================================ JobTech (SE)
JOBTECH_URL = "https://jobsearch.api.jobtechdev.se/search"
_JOBTECH_MODEL = {"distansarbete": "remote", "hybridarbete": "hybrid", "arbete pa plats": "onsite"}


def _workplace_label(label: Any, table: dict[str, str]) -> str | None:
    key = fold(str(label or "")).strip()
    if not key:
        return None
    if key in table:
        return table[key]
    if "hybrid" in key:
        return "hybrid"
    if any(neg in key for neg in ("ikke", "inte", "not ", "no ")):
        return None  # negated phrasing we don't know -> leave to the matcher
    if "remote" in key or "distans" in key or "hjemme" in key:
        return "remote"
    return None


@adapter("jobtech_se")
def jobtech_se(ctx: FetchContext) -> Iterator[Posting]:
    """Arbetsförmedlingen / JobTech JobSearch (open, no key): GET /search?q=&limit<=100&offset=.

    ``published-after`` takes minutes (deterministic URL → replayable fixtures).
    ``workplace_model`` is structured (Distansarbete / Hybridarbete / Arbete på plats)
    but empty on ~80 % of ads.
    """
    p = ctx.params
    queries = p.get("queries") or default_terms(6, ["sv"])
    limit = min(int(p.get("limit", 100)), 100)
    max_pages = int(p.get("max_pages", 1))
    minutes = int(float(p.get("published_within_days", 7)) * 1440)
    seen: set[str] = set()
    for q in queries:
        for page in range(max_pages):
            params = {"q": q, "limit": limit, "offset": page * limit, "sort": "pubdate-desc",
                      "published-after": minutes}
            data = ctx.http.get_json(f"{JOBTECH_URL}?{_qs(params)}")
            hits = data.get("hits") or []
            for h in hits:
                jid = str(h.get("id") or "")
                if not jid or jid in seen or h.get("removed"):
                    continue
                seen.add(jid)
                addrs = as_list(h.get("workplace_addresses")) or as_list(h.get("workplace_address"))
                countries, cities, raw_loc = [], [], []
                for a in addrs:
                    if not a:
                        continue
                    iso = country_iso(a.get("country"))
                    if iso and iso not in countries:
                        countries.append(iso)
                    city = a.get("city") or a.get("municipality")
                    if city and city not in cities:
                        cities.append(city)
                    bits = [x for x in (city, a.get("region"), a.get("country")) if x]
                    if bits:
                        raw_loc.append(", ".join(dict.fromkeys(bits)))
                desc = h.get("description") or {}
                employer = h.get("employer") or {}
                app = h.get("application_details") or {}
                labels = [(h.get(k) or {}).get("label") for k in ("employment_type", "working_hours_type", "duration")]
                yield Posting(
                    source=ctx.spec.id,
                    source_job_id=jid,
                    title=h.get("headline") or "",
                    company=employer.get("name") or employer.get("workplace") or "",
                    url=h.get("webpage_url") or f"https://arbetsformedlingen.se/platsbanken/annonser/{jid}",
                    apply_url=app.get("url") or None,
                    location_raw=" | ".join(dict.fromkeys(raw_loc)),
                    countries=countries,
                    cities=cities,
                    workplace=_workplace_label((h.get("workplace_model") or {}).get("label"), _JOBTECH_MODEL),
                    description=desc.get("text") or html_to_text(desc.get("text_formatted")),
                    posted_at=local_date(h.get("publication_date"), "Europe/Stockholm"),
                    employment_types=[x for x in labels if x],
                    tags=[x for x in ((h.get("occupation") or {}).get("label"),
                                      (h.get("occupation_field") or {}).get("label")) if x],
                    raw={"occupation": (h.get("occupation") or {}).get("label"),
                         "workplace_model": (h.get("workplace_model") or {}).get("label"),
                         "deadline": h.get("application_deadline"), "source_type": h.get("source_type")},
                )
            total = (data.get("total") or {}).get("value") or 0
            if len(hits) < limit or (page + 1) * limit >= total:
                break


# ============================================================ NAV arbeidsplassen (NO)
NAV_BASE = "https://pam-stilling-feed.nav.no"
_NAV_REMOTE = {"hjemmekontor": "remote", "kun hjemmekontor": "remote", "hybridkontor": "hybrid",
               "hjemmekontor ikke mulig": "onsite", "ikke hjemmekontor": "onsite"}


def _nav_token(ctx: FetchContext) -> str:
    token = (ctx.secrets or {}).get("NAV_FEED_TOKEN") or ""
    if token:
        return token
    if not ctx.params.get("use_public_token", True):
        raise RuntimeError("NAV_FEED_TOKEN missing and use_public_token is false")
    # NAV publishes a rotating token "for experiments"; a private one is free on request.
    text = ctx.http.get(f"{NAV_BASE}/api/publicToken").text
    for line in reversed(text.strip().splitlines()):
        if line.strip().startswith("ey"):
            return line.strip()
    raise RuntimeError("could not parse NAV public token")


@adapter("nav_feed")
def nav_feed(ctx: FetchContext) -> Iterator[Posting]:
    """NAV Job Vacancy Feed (pam-stilling-feed): a change feed, not a search API.

    1. GET /api/v1/feed with If-Modified-Since = now - lookback → first page (1000 items),
       follow ``next_url`` until it is null (≈1 400 changes/day → 2–3 pages/day).
    2. Items carry title/employer/municipality/status; only ACTIVE items whose title passes
       ``ctx.wants_detail`` (or the optional ``title_regex``) are fetched via /api/v1/feedentry/{id}.
    Contact data (contactList) is never kept (NAV terms of use).
    """
    import re

    p = ctx.params
    lookback_h = float(p.get("lookback_hours", 26))
    max_pages = int(p.get("max_pages", 6))
    max_details = int(p.get("max_details", 40))
    extra = re.compile(p["title_regex"], re.I) if p.get("title_regex") else None
    token = _nav_token(ctx)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_h)
    first_headers = {**headers, "If-Modified-Since": since.strftime("%a, %d %b %Y %H:%M:%S GMT")}
    latest: dict[str, dict] = {}
    url: str | None = f"{NAV_BASE}/api/v1/feed"
    pages = 0
    while url and pages < max_pages:
        resp = ctx.http.get(url, headers=first_headers if pages == 0 else headers)
        pages += 1
        if resp.status_code == 304:
            break
        if resp.status_code == 401:
            raise RuntimeError("NAV feed: 401 (token expired/rotated?)")
        resp.raise_for_status()
        page = resp.json()
        for item in page.get("items") or []:
            fe = item.get("_feed_entry") or {}
            uid = fe.get("uuid") or item.get("id")
            if uid:
                latest[uid] = item  # later entries supersede earlier ones
        nxt = page.get("next_url")
        url = f"{NAV_BASE}{nxt}" if nxt else None
    if url:
        ctx.notes.append(f"feed not exhausted after {max_pages} pages")
    details = 0
    for uid, item in latest.items():
        fe = item.get("_feed_entry") or {}
        title = item.get("title") or fe.get("title") or ""
        if fe.get("status") != "ACTIVE":
            continue
        if not (ctx.wants_detail(title) or (extra and extra.search(title))):
            ctx.skipped += 1
            continue
        if details >= max_details or ctx.http.remaining <= 1:
            ctx.notes.append("detail cap reached")
            break
        resp = ctx.http.get(f"{NAV_BASE}{item.get('url') or '/api/v1/feedentry/' + uid}", headers=headers)
        details += 1
        if resp.status_code != 200:
            ctx.notes.append(f"feedentry {uid}: HTTP {resp.status_code}")
            continue
        entry = resp.json()
        ad = entry.get("ad_content") or {}
        if entry.get("status") not in (None, "ACTIVE"):
            continue
        countries, cities, raw_loc = [], [], []
        for w in as_list(ad.get("workLocations")):
            iso = country_iso(w.get("country"))
            if iso and iso not in countries:
                countries.append(iso)
            city = (w.get("city") or w.get("municipal") or "").title()
            if city and city not in cities:
                cities.append(city)
            bits = [x for x in (city, (w.get("county") or "").title(), (w.get("country") or "").title()) if x]
            if bits:
                raw_loc.append(", ".join(dict.fromkeys(bits)))
        employer = ad.get("employer") or {}
        yield Posting(
            source=ctx.spec.id,
            source_job_id=uid,
            title=ad.get("title") or title,
            company=employer.get("name") or fe.get("businessName") or "",
            url=ad.get("link") or f"https://arbeidsplassen.nav.no/stillinger/stilling/{uid}",
            apply_url=ad.get("applicationUrl") or ad.get("sourceurl") or None,
            location_raw=" | ".join(dict.fromkeys(raw_loc)) or (fe.get("municipal") or "").title(),
            countries=countries,
            cities=cities,
            workplace=_workplace_label(ad.get("remote"), _NAV_REMOTE),
            description=html_to_text(ad.get("description")),
            posted_at=parse_date(ad.get("published")),
            employment_types=[x for x in (ad.get("engagementtype"), ad.get("extent")) if x],
            tags=[c.get("name") for c in as_list(ad.get("categoryList"))
                  if c.get("categoryType") in ("ESCO", "STYRK08") and c.get("name")],
            raw={"jobtitle": ad.get("jobtitle"), "remote": ad.get("remote"), "source": ad.get("source"),
                 "expires": ad.get("expires"), "sector": ad.get("sector")},
        )


# ===================================================================== EURES (EU)
EURES_BASE = "https://europa.eu/eures/api/jv-searchengine"
_EURES_ISO_FIX = {"EL": "GR", "UK": "GB"}


def _eures_body(keyword: str, page: int, per_page: int, period: str, locations: list[str]) -> dict[str, Any]:
    # Mirrors what the EURES portal (europa.eu/eures/portal/jv-se) POSTs.
    return {
        "resultsPerPage": per_page, "page": page, "sortSearch": "BEST_MATCH",
        "keywords": [{"keyword": keyword, "specificSearchCode": "TITLE"}],
        "publicationPeriod": period, "occupationUris": [], "skillUris": [], "requiredExperienceCodes": [],
        "positionScheduleCodes": [], "sectorCodes": [], "educationAndQualificationLevelCodes": [],
        "positionOfferingCodes": [], "locationCodes": locations, "euresFlagCodes": [], "otherBenefitsCodes": [],
        "requiredLanguages": [], "minNumberPost": None,
    }


@adapter("eures")
def eures(ctx: FetchContext) -> Iterator[Posting]:
    """EURES job-vacancy search (the portal's own public JSON backend, no key).

    POST /eures/api/jv-searchengine/public/jv-search/search. Keyword matching is word-OR
    (quotes ignored), so we sort by BEST_MATCH and stop paging once a page is mostly
    off-target titles. Search results already carry the full description text.
    """
    p = ctx.params
    keywords = p.get("keywords") or default_terms(5)
    per_page = min(int(p.get("per_page", 50)), 50)
    max_pages = int(p.get("max_pages", 2))
    period = p.get("publication_period", "LAST_WEEK")  # LAST_DAY|LAST_THREE_DAYS|LAST_WEEK|LAST_MONTH
    locations = list(p.get("location_codes") or [])
    min_share = float(p.get("min_relevant_share", 0.3))
    lang = p.get("lang", "en")
    url = f"{EURES_BASE}/public/jv-search/search"
    seen: set[str] = set()
    failed = 0
    for kw in keywords:
        for page in range(1, max_pages + 1):
            try:
                data = ctx.http.post_json(url, json=_eures_body(kw, page, per_page, period, locations))
            except httpx.HTTPStatusError as exc:
                # one bad keyword (EURES answers 500 now and then) shouldn't cost the whole run
                if exc.response.status_code < 500:
                    raise
                ctx.notes.append(f"HTTP {exc.response.status_code} on keyword {keywords.index(kw) + 1}")
                failed += page == 1
                break
            jvs = data.get("jvs") or []
            relevant = 0
            for j in jvs:
                jid = str(j.get("id") or "")
                title = (j.get("title") or "").strip()
                if ctx.wants_detail(title):
                    relevant += 1
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                loc_map = j.get("locationMap") or {}
                countries = []
                for code in loc_map:
                    iso = _EURES_ISO_FIX.get(str(code).upper(), str(code).upper())
                    if len(iso) == 2 and iso not in countries:
                        countries.append(iso)
                nuts = sorted({n for codes in loc_map.values() for n in (codes or []) if n})
                employer = j.get("employer") or {}
                yield Posting(
                    source=ctx.spec.id,
                    source_job_id=jid,
                    title=title,
                    company=employer.get("name") or "",
                    url=f"https://europa.eu/eures/portal/jv-se/jv-details/{jid}?lang={lang}",
                    location_raw=", ".join(countries),
                    countries=countries,
                    description=html_to_text(j.get("description")),
                    posted_at=parse_date(j.get("creationDate")),
                    employment_types=[x for x in as_list(j.get("positionScheduleCodes")) + [j.get("positionOfferingCode")] if x],
                    tags=as_list(j.get("availableLanguages")),
                    raw={"nuts": nuts, "eures_flag": j.get("euresFlag"), "esco": as_list(j.get("jobCategoriesCodes"))[:3],
                         "last_modified": j.get("lastModificationDate")},
                )
            if len(jvs) < per_page or (jvs and relevant / len(jvs) < min_share):
                break
    if failed == len(keywords):
        raise RuntimeError(f"EURES search failed for all {failed} keywords (HTTP 5xx)")
