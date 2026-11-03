"""Job boards with open public JSON APIs: The Muse, Landing.jobs, Get on Board."""

from __future__ import annotations

import re
from typing import Any, Iterator
from urllib.parse import quote, urlencode

from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, default_terms, as_list, first, parse_date, to_float
from jobradar.sources.boards.national_apis import country_iso

_US_STATE = re.compile(r"^[A-Z]{2}$")


def _country_from_place(place: str) -> str | None:
    """'Berlin, Germany' → DE; 'Austin, TX' → US; 'Flexible / Remote' → None."""
    parts = [x.strip() for x in place.split(",") if x.strip()]
    if len(parts) < 2:
        return None
    last = parts[-1]
    if _US_STATE.match(last):
        return "US"
    iso = country_iso(last)
    if iso:
        return iso
    try:  # fall back to the shared multilingual country table
        from jobradar.normalize.location import _country_names
        from jobradar.normalize.text import fold

        return _country_names().get(fold(last))
    except Exception:  # noqa: BLE001
        return None


# ================================================================= The Muse
MUSE_URL = "https://www.themuse.com/api/public/jobs"
MUSE_CATEGORIES = ["Software Engineering"]   # fallback when no pack names The Muse categories
MUSE_LOCATIONS = [
    "Flexible / Remote", "Warsaw, Poland", "Berlin, Germany", "Munich, Germany",
    "Amsterdam, Netherlands", "Dublin, Ireland", "London, United Kingdom", "Paris, France",
    "Madrid, Spain", "Barcelona, Spain", "Lisbon, Portugal", "Prague, Czech Republic",
]


@adapter("themuse")
def themuse(ctx: FetchContext) -> Iterator[Posting]:
    """The Muse public API (no key: 500 req/h). 20 jobs/page, no date sort, so we walk
    ``max_pages`` pages of the (category × location) result set. ``location`` and
    ``category`` are repeatable query params (OR)."""
    p = ctx.params
    from jobradar import config

    cats = p.get("categories") or config.board_map("themuse").get("categories") or MUSE_CATEGORIES
    locs = p.get("locations") or MUSE_LOCATIONS
    levels = p.get("levels") or []  # e.g. ["Mid Level", "Senior Level"]
    max_pages = int(p.get("max_pages", 10))
    key = (ctx.secrets or {}).get("THEMUSE_API_KEY")
    seen: set[str] = set()
    for page in range(max_pages):
        q: list[tuple[str, Any]] = [("page", page)]
        q += [("category", c) for c in cats] + [("location", l) for l in locs] + [("level", lv) for lv in levels]
        if key:
            q.append(("api_key", key))
        data = ctx.http.get_json(f"{MUSE_URL}?{urlencode(q, quote_via=quote)}")
        for j in data.get("results") or []:
            jid = str(j.get("id") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            places = [l.get("name") or "" for l in as_list(j.get("locations"))]
            remote = any("remote" in x.lower() or "flexible" in x.lower() for x in places)
            countries: list[str] = []
            cities: list[str] = []
            for place in places:
                iso = _country_from_place(place)
                if iso and iso not in countries:
                    countries.append(iso)
                if iso and "," in place:
                    cities.append(place.split(",")[0].strip())
            yield Posting(
                source=ctx.spec.id,
                source_job_id=jid,
                title=(j.get("name") or "").strip(),
                company=(j.get("company") or {}).get("name") or "",
                url=(j.get("refs") or {}).get("landing_page") or f"https://www.themuse.com/jobs/{jid}",
                location_raw=" | ".join(places),
                countries=countries,
                cities=cities,
                workplace="remote" if remote and len(places) == 1 else None,
                description=html_to_text(j.get("contents")),
                posted_at=parse_date(j.get("publication_date")),
                tags=[c.get("name") for c in as_list(j.get("categories")) if c.get("name")]
                + [lv.get("name") for lv in as_list(j.get("levels")) if lv.get("name")],
                raw={"company_short": (j.get("company") or {}).get("short_name"), "type": j.get("type")},
            )
        if page + 1 >= int(data.get("page_count") or 0):
            break


# ============================================================== Landing.jobs
LJ_BASE = "https://landing.jobs"
_LJ_SITEMAP_JOB = re.compile(
    r"<url>\s*<loc>\s*(https://landing\.jobs/at/([^/<]+)/([^<\s]+))\s*</loc>(.*?)</url>", re.S)
_LJ_SLUG_TAIL = re.compile(r"(?:-in-[a-z0-9-]+|-\d{4}(?:-\d+)?|-\d+)$")
_LD_JSON = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)


def _slug_title(slug: str) -> str:
    prev = None
    while prev != slug:
        prev, slug = slug, _LJ_SLUG_TAIL.sub("", slug)
    return slug.replace("-", " ")


def jsonld_jobposting(page: str) -> dict[str, Any] | None:
    """First schema.org JobPosting object in a page's JSON-LD blocks."""
    import json

    for block in _LD_JSON.findall(page):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop(0)
            if not isinstance(node, dict):
                continue
            if node.get("@type") == "JobPosting":
                return node
            stack.extend(as_list(node.get("@graph")))
    return None


def _jobposting_to_posting(ctx: FetchContext, jp: dict[str, Any], jid: str, url: str, **extra: Any) -> Posting:
    countries, cities, raw_loc = [], [], []
    for place in as_list(jp.get("jobLocation")):
        addr = (place or {}).get("address") or {}
        if isinstance(addr, str):
            raw_loc.append(addr)
            continue
        iso = country_iso(addr.get("addressCountry"))
        if iso and iso not in countries:
            countries.append(iso)
        city = addr.get("addressLocality")
        if city and city not in cities:
            cities.append(city)
        raw_loc.append(", ".join(x for x in (city, addr.get("addressCountry")) if x))
    remote = str(jp.get("jobLocationType") or "").upper() == "TELECOMMUTE"
    salary = jp.get("baseSalary") or {}
    value = salary.get("value") if isinstance(salary, dict) else {}
    value = value if isinstance(value, dict) else {}
    org = jp.get("hiringOrganization") or {}
    return Posting(
        source=ctx.spec.id,
        source_job_id=jid,
        title=jp.get("title") or "",
        company=org.get("name") if isinstance(org, dict) else str(org or ""),
        url=url,
        location_raw=" | ".join(dict.fromkeys(x for x in raw_loc if x)),
        countries=countries,
        cities=cities,
        workplace="remote" if remote else None,
        description=html_to_text(jp.get("description")),
        posted_at=parse_date(jp.get("datePosted")),
        employment_types=[str(x) for x in as_list(jp.get("employmentType")) if x],
        salary_min=to_float(first(value.get("minValue"), value.get("value"))),
        salary_max=to_float(value.get("maxValue")),
        salary_currency=salary.get("currency") if isinstance(salary, dict) else None,
        salary_period=str(value.get("unitText") or "").lower() or None if value else None,
        **extra,
    )


@adapter("landingjobs")
def landingjobs(ctx: FetchContext) -> Iterator[Posting]:
    """Landing.jobs (PT/EU tech board).

    mode=sitemap (default, robots-compliant): /sitemap.xml lists every live job as
    /at/{company}/{slug}; the slug gives a title guess for the ``wants_detail`` gate, and
    the job page's schema.org JobPosting JSON-LD gives the posting.
    mode=api: GET /api/v1/jobs?limit=&offset= (undocumented JSON; robots.txt disallows
    /api/, so only use it with the site's permission).
    """
    p = ctx.params
    if p.get("mode", "sitemap") == "api":
        yield from _landingjobs_api(ctx)
        return
    max_details = int(p.get("max_details", 25))
    resp = ctx.http.get(f"{LJ_BASE}/sitemap.xml")
    resp.raise_for_status()
    done = 0
    for url, company_slug, slug, rest in _LJ_SITEMAP_JOB.findall(resp.text):
        if not ctx.wants_detail(_slug_title(slug)):
            ctx.skipped += 1
            continue
        if ctx.is_known(ctx.spec.id, f"{company_slug}/{slug}"):
            ctx.mark_seen(ctx.spec.id, f"{company_slug}/{slug}")
            continue
        if done >= max_details or ctx.http.remaining < 1:
            ctx.notes.append("detail cap reached")
            break
        page = ctx.http.get(url)
        done += 1
        if page.status_code != 200:
            ctx.notes.append(f"{slug}: HTTP {page.status_code}")
            continue
        jp = jsonld_jobposting(page.text)
        if not jp:
            ctx.notes.append(f"{slug}: no JobPosting JSON-LD")
            continue
        lastmod = re.search(r"<lastmod>(.*?)</lastmod>", rest)
        yield _jobposting_to_posting(
            ctx, jp, f"{company_slug}/{slug}", url,
            raw={"lastmod": lastmod.group(1) if lastmod else None, "validThrough": jp.get("validThrough"),
                 "experience_months": (jp.get("experienceRequirements") or {}).get("monthsOfExperience")
                 if isinstance(jp.get("experienceRequirements"), dict) else None},
        )


def _landingjobs_api(ctx: FetchContext) -> Iterator[Posting]:
    limit = 50
    for page in range(int(ctx.params.get("max_pages", 4))):
        rows = ctx.http.get_json(f"{LJ_BASE}/api/v1/jobs?limit={limit}&offset={page * limit}")
        for j in rows or []:
            url = j.get("url") or ""
            locs = as_list(j.get("locations"))
            countries = list(dict.fromkeys(c for c in (country_iso(l.get("country_code")) for l in locs) if c))
            parts = url.rstrip("/").split("/")
            yield Posting(
                source=ctx.spec.id,
                source_job_id="/".join(parts[-2:]) if len(parts) > 2 else str(j.get("id")),
                title=j.get("title") or "",
                company="",
                url=url,
                location_raw=" | ".join(f"{l.get('city')}, {l.get('country_code')}" for l in locs),
                countries=countries,
                cities=[l.get("city") for l in locs if l.get("city")],
                workplace="remote" if j.get("remote") else None,
                description="\n\n".join(html_to_text(j.get(k)) for k in ("role_description", "main_requirements",
                                                                          "nice_to_have", "perks") if j.get(k)),
                posted_at=parse_date(j.get("published_at")),
                employment_types=as_list(j.get("type")),
                salary_min=to_float(j.get("gross_salary_low")),
                salary_max=to_float(j.get("gross_salary_high")),
                salary_currency=j.get("currency_code") if j.get("gross_salary_low") else None,
                salary_period="year" if j.get("gross_salary_low") else None,
                tags=as_list(j.get("tags")),
                raw={"id": j.get("id"), "company_id": j.get("company_id"), "expires_at": j.get("expires_at")},
            )
        if len(rows or []) < limit:
            break


# ============================================================== Get on Board
GOB_URL = "https://www.getonbrd.com/api/v0/search/jobs"
_GOB_MODE = {"fully_remote": "remote", "remote_local": "remote", "temporarily_remote": None,
             "hybrid": "hybrid", "no_remote": "onsite"}
_GOB_EXPAND = '["company","location_tenants","location_cities","tags","seniority"]'


def _gob_rel(attr: Any) -> list[dict]:
    data = (attr or {}).get("data") if isinstance(attr, dict) else None
    return [x for x in as_list(data) if isinstance(x, dict)]


def _iso_any(name: Any) -> str | None:
    iso = country_iso(name)
    if iso:
        return iso
    try:
        from jobradar.normalize.location import _country_names
        from jobradar.normalize.text import fold

        return _country_names().get(fold(str(name or "")))
    except Exception:  # noqa: BLE001
        return None


@adapter("getonbrd")
def getonbrd(ctx: FetchContext) -> Iterator[Posting]:
    """Get on Board public API (LATAM-centric tech board, no key).

    GET /api/v0/search/jobs?query=&per_page=&page=&expand=[...]; results come newest
    first, so paging stops once a page's oldest job is older than ``max_age_days``.
    ``remote_modality``: fully_remote | remote_local (remote for residents of the listed
    countries) | hybrid | no_remote.
    """
    from datetime import datetime, timedelta, timezone

    p = ctx.params
    queries = p.get("queries") or default_terms(5)
    per_page = min(int(p.get("per_page", 50)), 100)
    max_pages = int(p.get("max_pages", 2))
    cutoff = datetime.now(timezone.utc) - timedelta(days=float(p.get("max_age_days", 30)))
    seen: set[str] = set()
    for q in queries:
        for page in range(1, max_pages + 1):
            url = f"{GOB_URL}?{urlencode({'query': q, 'per_page': per_page, 'page': page, 'expand': _GOB_EXPAND}, quote_via=quote)}"
            data = ctx.http.get_json(url)
            rows = data.get("data") or []
            oldest = None
            for j in rows:
                a = j.get("attributes") or {}
                jid = str(j.get("id") or "")
                posted = parse_date(a.get("published_at"))
                oldest = posted if oldest is None or (posted and posted < oldest) else oldest
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                mode = a.get("remote_modality") or ("fully_remote" if a.get("remote") else None)
                tenants = [(t.get("attributes") or {}).get("name") for t in _gob_rel(a.get("location_tenants"))]
                city_rows = [t.get("attributes") or {} for t in _gob_rel(a.get("location_cities"))]
                countries: list[str] = []
                for name in tenants + [c.get("country") for c in city_rows]:
                    iso = _iso_any(name)
                    if iso and iso not in countries:
                        countries.append(iso)
                cities = [c.get("name") for c in city_rows if c.get("name")]
                loc_bits = [f"{c.get('name')}, {c.get('country')}" for c in city_rows if c.get("name")]
                loc_bits += [x for x in as_list(a.get("countries")) if x and x not in ("Remote",)]
                if mode in ("fully_remote", "remote_local"):
                    loc_bits.insert(0, "Remote")
                company = ((a.get("company") or {}).get("data") or {}).get("attributes") or {}
                seniority = ((a.get("seniority") or {}).get("data") or {}).get("attributes") or {}
                sal_min, sal_max = to_float(a.get("min_salary")), to_float(a.get("max_salary"))
                yield Posting(
                    source=ctx.spec.id,
                    source_job_id=jid,
                    title=(a.get("title") or "").strip(),
                    company=company.get("name") or "",
                    url=(j.get("links") or {}).get("public_url") or f"https://www.getonbrd.com/jobs/{jid}",
                    location_raw=" | ".join(dict.fromkeys(str(x) for x in loc_bits if x)),
                    countries=countries,
                    cities=cities,
                    workplace=_GOB_MODE.get(str(mode)) if mode else None,
                    remote_scope=(a.get("remote_zone") or (", ".join(t for t in tenants if t) if mode == "remote_local" else None)) or None,
                    description="\n\n".join(html_to_text(a.get(k)) for k in ("functions", "description", "desirable", "benefits")
                                            if a.get(k)),
                    posted_at=posted,
                    salary_min=sal_min,
                    salary_max=sal_max,
                    salary_currency="USD" if (sal_min or sal_max) else None,
                    salary_period="month" if (sal_min or sal_max) else None,
                    tags=[(t.get("attributes") or {}).get("name") for t in _gob_rel(a.get("tags"))
                          if (t.get("attributes") or {}).get("name")][:15] + ([seniority["name"]] if seniority.get("name") else []),
                    raw={"remote_modality": mode, "lang": a.get("lang"), "category": a.get("category_name"),
                         "tenants": tenants},
                )
            if len(rows) < per_page or page >= int((data.get("meta") or {}).get("total_pages") or 0) \
                    or (oldest is not None and oldest < cutoff):
                break
