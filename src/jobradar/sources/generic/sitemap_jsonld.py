"""Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting.

Many boards disallow their JSON APIs in robots.txt but publish an "active jobs" sitemap and embed a
schema.org ``JobPosting`` (JSON-LD) in every offer page for Google for Jobs. This adapter uses only
those two public surfaces:

1. fetch the sitemap(s) (``sitemap_index`` and/or ``sitemaps``; nested indexes and ``.xml.gz`` handled),
2. keep job URLs that pass ``url_include`` / ``url_exclude`` regexes, are newer than ``lastmod_days``,
   are not already stored (``ctx.known``) and whose slug looks like a target title
   (``ctx.wants_detail`` on the slug text — the matcher's cheap title gate),
3. fetch at most ``max_details`` detail pages (priority by ``prefer`` regexes, then newest lastmod),
4. map the JSON-LD JobPosting to ``Posting``; an optional per-site ``enrich`` hook can add fields the
   JSON-LD lacks (work mode, contract types, salary per contract…) from the same HTML page.

Params (all optional except a sitemap):
  sitemap_index: url | [urls]      sitemap index(es); children filtered by ``sitemap_include``
  sitemaps: url | [urls]           urlset sitemaps (an index here is expanded too)
  sitemap_include: regex           which child sitemaps of an index to read (default: all)
  url_include / url_exclude: [regex]  applied to the job URL (search, case-insensitive)
  id_regex: regex with (?P<id>…)   source_job_id from the URL (default: last path segment)
  slug_regex: regex with (?P<slug>…)  slug used for the title gate (default: last path segment)
  slug_strip: [regex]              removed from the slug before the gate (e.g. trailing category)
  group_strip: regex               URLs whose slug minus this regex coincide are one job (multi-city
                                   copies); only one per group is fetched, none if any is known
  title_gate: bool (true)          use ctx.wants_detail(slug text)
  lastmod_days: int                skip URLs whose <lastmod> is older
  prefer: [regex]                  fetch order: URLs matching earlier regexes first
  newest_id_first: bool (false)    sitemaps without lastmod: within a prefer bucket, higher numeric job ids first
  max_details: int (40)            detail pages per run
  skip_known: bool (true)          don't refetch postings already stored
  skip_expired: bool (true)        drop postings whose validThrough is in the past
  default_currency: str            when baseSalary has no currency
  default_company: str             when hiringOrganization has no name (e.g. a bare {"@id": ...} graph ref)
  listing_urls: url | [urls]       listing pages instead of (or besides) a sitemap: job links are scraped from
                                   the HTML (no lastmod); {page} in a URL is expanded for pages 1..listing_pages
  listing_link_regex: regex        which hrefs of a listing page are job links (default: url_include / all)
  listing_pages: int (1)           pages per listing URL that contains {page}
  enrich: name                     per-site hook registered with ``@enricher(name)``
"""

from __future__ import annotations

import gzip
import html as html_lib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import unquote, urljoin, urlsplit

from jobradar.http.client import Blocked
from jobradar.models import Posting
from jobradar.normalize.text import fold, html_to_text
from jobradar.sources.base import FetchContext, adapter, as_list, first, parse_date, to_float

# ------------------------------------------------------------------ enrichers
# fn(posting, page_html, ctx) -> extra job URLs covered by this page (e.g. other-city copies) or None
Enricher = Callable[[Posting, str, FetchContext], "list[str] | None"]
ENRICHERS: dict[str, Enricher] = {}


def enricher(name: str) -> Callable[[Enricher], Enricher]:
    def deco(fn: Enricher) -> Enricher:
        ENRICHERS[name] = fn
        return fn

    return deco


def _get_enricher(name: str | None) -> Enricher | None:
    if not name:
        return None
    if name not in ENRICHERS:
        from jobradar.sources.registry import all_adapters  # lazy: board modules register enrichers

        all_adapters()
    if name not in ENRICHERS:
        raise KeyError(f"unknown enricher {name!r}; known: {sorted(ENRICHERS)}")
    return ENRICHERS[name]


# ------------------------------------------------------------------ sitemaps
@dataclass
class SitemapEntry:
    loc: str
    lastmod: datetime | None


_BLOCK = re.compile(r"<(url|sitemap)\b[^>]*>(.*?)</\1>", re.S | re.I)
_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S | re.I)
_LASTMOD = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.S | re.I)


def decode_body(content: bytes) -> str:
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    return content.decode("utf-8-sig", errors="replace")


def _uncdata(value: str) -> str:
    return value.replace("<![CDATA[", "").replace("]]>", "").strip()


def parse_sitemap(text: str) -> tuple[list[SitemapEntry], list[SitemapEntry]]:
    """Return (child sitemaps, urls) from a sitemap index or urlset document."""
    children: list[SitemapEntry] = []
    urls: list[SitemapEntry] = []
    for m in _BLOCK.finditer(text):
        kind, body = m.group(1).lower(), m.group(2)
        loc_m = _LOC.search(body)
        if not loc_m:
            continue
        loc = html_lib.unescape(_uncdata(loc_m.group(1)))
        lm = _LASTMOD.search(body)
        entry = SitemapEntry(loc, parse_date(_uncdata(lm.group(1))) if lm else None)
        (children if kind == "sitemap" else urls).append(entry)
    return children, urls


_HREF = re.compile(r"""href=["']([^"'#]+)""", re.I)


def collect_listing_urls(ctx: FetchContext) -> tuple[list[SitemapEntry], int]:
    """Job links scraped from listing pages (``listing_urls``) → (entries without lastmod, pages fetched)."""
    p = ctx.params
    link_rx = re.compile(p["listing_link_regex"], re.I) if p.get("listing_link_regex") else None
    pages = int(p.get("listing_pages", 1))
    out: dict[str, SitemapEntry] = {}
    fetched = 0
    for tpl in as_list(p.get("listing_urls")):
        tpl = re.sub(r"(?<=remote_in=)[A-Z]{2}", lambda m: m.group().lower(), tpl)   # {country} expands upper-case
        urls = [tpl.format(page=i) for i in range(1, pages + 1)] if "{page}" in tpl else [tpl]
        for url in urls:
            try:
                resp = ctx.http.get(url)
            except Blocked as exc:
                if not exc.reason.startswith("robots"):
                    raise
                ctx.notes.append(f"listing {url}: disallowed by robots.txt")
                break
            fetched += 1
            if resp.status_code != 200:
                ctx.notes.append(f"listing {url}: HTTP {resp.status_code}")
                break
            found = 0
            for m in _HREF.finditer(resp.text):
                href = html_lib.unescape(m.group(1)).strip()
                if link_rx and not link_rx.search(href):
                    continue
                full = urljoin(url, href).split("?", 1)[0]
                if full not in out:
                    out[full] = SitemapEntry(full, None)
                    found += 1
            if not found:
                break
    return list(out.values()), fetched


def collect_sitemap_urls(ctx: FetchContext, *, max_docs: int = 20) -> tuple[list[SitemapEntry], int]:
    """Fetch the configured sitemaps (expanding indexes) → (url entries, documents fetched)."""
    p = ctx.params
    include = re.compile(p["sitemap_include"], re.I) if p.get("sitemap_include") else None
    queue: list[str] = as_list(p.get("sitemap_index")) + as_list(p.get("sitemaps"))
    seen_docs: set[str] = set()
    out: dict[str, SitemapEntry] = {}
    docs = 0
    while queue and docs < max_docs:
        url = queue.pop(0)
        if url in seen_docs:
            continue
        seen_docs.add(url)
        try:
            resp = ctx.http.get(url)
        except Blocked as exc:
            if not exc.reason.startswith("robots"):
                raise
            ctx.notes.append(f"sitemap {url}: disallowed by robots.txt")
            continue
        docs += 1
        if resp.status_code != 200:
            ctx.notes.append(f"sitemap {url}: HTTP {resp.status_code}")
            continue
        children, urls = parse_sitemap(decode_body(resp.content))
        for child in children:
            if include is None or include.search(child.loc):
                queue.append(child.loc)
        for e in urls:
            out.setdefault(e.loc, e)
    return list(out.values()), docs


# ------------------------------------------------------------------ JSON-LD
_LD = re.compile(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I)


def _iter_ld(obj: Any) -> Iterator[dict]:
    if isinstance(obj, list):
        for x in obj:
            yield from _iter_ld(x)
    elif isinstance(obj, dict):
        yield obj
        if "@graph" in obj:
            yield from _iter_ld(obj["@graph"])


def _is_type(obj: dict, name: str) -> bool:
    return name in [str(t) for t in as_list(obj.get("@type"))]


def extract_jobposting(page: str) -> dict | None:
    """First schema.org JobPosting found in the page's JSON-LD blocks (``@graph`` and lists handled)."""
    for m in _LD.finditer(page):
        raw = m.group(1).strip()
        if raw.startswith("<!--"):
            raw = raw.strip("<!->").strip()
        try:
            data = json.loads(raw, strict=False)
        except ValueError:
            try:
                data = json.loads(html_lib.unescape(raw), strict=False)
            except ValueError:
                continue
        for obj in _iter_ld(data):
            if _is_type(obj, "JobPosting"):
                return obj
    return None


# ------------------------------------------------------------------ mapping helpers
_DATA = Path(__file__).parents[2] / "normalize" / "data" / "countries.tsv"
_ISO3 = {
    "POL": "PL", "DEU": "DE", "GBR": "GB", "USA": "US", "UKR": "UA", "CZE": "CZ", "SVK": "SK", "HUN": "HU",
    "NLD": "NL", "FRA": "FR", "ESP": "ES", "PRT": "PT", "ITA": "IT", "AUT": "AT", "CHE": "CH", "BEL": "BE",
    "IRL": "IE", "SWE": "SE", "NOR": "NO", "DNK": "DK", "FIN": "FI", "LTU": "LT", "LVA": "LV", "EST": "EE",
    "ROU": "RO", "BGR": "BG", "HRV": "HR", "SVN": "SI", "GRC": "GR", "CYP": "CY", "LUX": "LU", "MLT": "MT",
}
_ALIASES = {
    "polska": "PL", "poland": "PL", "pl": "PL", "niemcy": "DE", "germany": "DE", "deutschland": "DE",
    "wielka brytania": "GB", "united kingdom": "GB", "uk": "GB", "england": "GB", "czechy": "CZ",
    "czech republic": "CZ", "czechia": "CZ", "ukraina": "UA", "hiszpania": "ES", "portugalia": "PT",
    "holandia": "NL", "francja": "FR", "wlochy": "IT", "usa": "US", "stany zjednoczone": "US",
    "united states": "US", "rumunia": "RO", "litwa": "LT", "slowacja": "SK", "wegry": "HU", "austria": "AT",
    "szwajcaria": "CH", "irlandia": "IE", "szwecja": "SE", "dania": "DK", "norwegia": "NO", "finlandia": "FI",
}


@lru_cache(maxsize=1)
def _country_table() -> dict[str, str]:
    table: dict[str, str] = {}
    try:
        for line in _DATA.read_text(encoding="utf-8").splitlines():
            iso, _, name = line.partition("\t")
            if iso and name:
                table[fold(name)] = iso
    except OSError:
        pass
    table.update(_ALIASES)
    return table


def country_iso2(value: Any) -> str | None:
    """'PL' / 'POL' / 'Poland' / 'Polska' / {'name': ...} → 'PL'."""
    if isinstance(value, dict):
        value = first(value.get("identifier"), value.get("alternateName"), value.get("name"), value.get("code"))
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if len(v) == 2 and v.isalpha():
        return v.upper()
    if len(v) == 3 and v.isupper() and v in _ISO3:
        return _ISO3[v]
    return _country_table().get(fold(v))


_PERIOD = {"HOUR": "hour", "DAY": "day", "MONTH": "month", "YEAR": "year", "ANNUAL": "year", "YEARLY": "year",
           "MONTHLY": "month", "HOURLY": "hour", "DAILY": "day"}


def salary_period(unit: Any) -> str | None:
    return _PERIOD.get(str(unit or "").strip().upper())


_B2B = re.compile(r"\bb2b\b|kontrakt b2b|umowa b2b", re.I)
_UOP = re.compile(r"umow\w* o prac\w*|\buop\b|contract of employment|employment contract|permanent contract", re.I)
_UZ = re.compile(r"umow\w* zleceni\w*|mandate contract|contract of mandate", re.I)


def contract_tags(*texts: str, employment_types: list[str] | None = None) -> list[str]:
    """Polish contract forms as tags: b2b / uop (umowa o pracę) / uz (umowa zlecenie)."""
    blob = "\n".join(t for t in texts if t)[:20000]
    tags: list[str] = []
    ets = {str(e).upper() for e in employment_types or []}
    if _B2B.search(blob) or "CONTRACTOR" in ets or "B2B" in ets:
        tags.append("b2b")
    if _UOP.search(blob) or "PERMANENT" in ets:
        tags.append("uop")
    if _UZ.search(blob) or "MANDATE_CONTRACT" in ets:
        tags.append("uz")
    return tags


def _place_parts(place: Any) -> tuple[str | None, str | None, str]:
    """(city, iso2, label) from a schema.org Place / PostalAddress / string."""
    if isinstance(place, str):
        return None, country_iso2(place), place
    if not isinstance(place, dict):
        return None, None, ""
    addr = place.get("address") if "address" in place else place
    if isinstance(addr, str):
        return None, None, addr
    if not isinstance(addr, dict):
        return None, None, str(place.get("name") or "")
    city = (addr.get("addressLocality") or "").strip() or None
    country_raw = addr.get("addressCountry")
    iso = country_iso2(country_raw)
    country_label = country_raw.get("name") if isinstance(country_raw, dict) else country_raw
    if isinstance(country_label, str) and len(country_label.strip()) <= 3 and iso:
        country_label = iso
    label = ", ".join(x for x in (city, (addr.get("addressRegion") or "").strip() or None, country_label) if x)
    return city, iso, label


def _skills(value: Any) -> list[str]:
    out: list[str] = []
    for s in as_list(value):
        if isinstance(s, dict):
            s = first(s.get("value"), s.get("name"))
        if isinstance(s, str):
            out.extend(x.strip() for x in s.split(",") if x.strip())
    return out


_COMPACT_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2}))?$")


def _compact_date(value: Any) -> Any:
    """'20250701T084614' (Experis) → ISO; anything else unchanged."""
    m = _COMPACT_DATE.match(str(value or "").strip())
    if not m:
        return value
    y, mo, d, h, mi, sec = m.groups()
    return f"{y}-{mo}-{d}" + (f"T{h}:{mi}:{sec}" if h else "")


def jobposting_to_posting(
    jp: dict,
    *,
    source: str,
    source_job_id: str,
    url: str,
    default_currency: str | None = None,
    default_company: str | None = None,
) -> Posting:
    org = jp.get("hiringOrganization")
    company = org.get("name") if isinstance(org, dict) else org
    if not isinstance(company, str) or not company.strip():
        company = default_company
    cities: list[str] = []
    countries: list[str] = []
    labels: list[str] = []
    for place in as_list(jp.get("jobLocation")):
        city, iso, label = _place_parts(place)
        if city and city not in cities:
            cities.append(city)
        if iso and iso not in countries:
            countries.append(iso)
        if label and label not in labels:
            labels.append(label)
    loc_types = {str(t).upper() for t in as_list(jp.get("jobLocationType"))}
    workplace = "remote" if "TELECOMMUTE" in loc_types else None
    # applicantLocationRequirements: meaningful only when short (NoFluffJobs lists every country on earth)
    req_names: list[str] = []
    req_isos: list[str] = []
    reqs = as_list(jp.get("applicantLocationRequirements"))
    if 0 < len(reqs) <= 10:
        for r in reqs:
            name = r.get("name") if isinstance(r, dict) else r
            if isinstance(name, str) and name.strip():
                req_names.append(name.strip())
                iso = country_iso2(r)
                if iso and iso not in req_isos:
                    req_isos.append(iso)
    if not countries:
        countries = req_isos
    remote_scope = ", ".join(req_names) if workplace == "remote" and req_names else None
    if workplace == "remote" and not labels:
        labels.append("Remote" + (f" ({remote_scope})" if remote_scope else ""))

    smin = smax = None
    currency = period = None
    for amount in as_list(jp.get("baseSalary"))[:1]:
        if not isinstance(amount, dict):
            smin = smax = to_float(amount)
            continue
        currency = amount.get("currency")
        val = amount.get("value")
        if isinstance(val, dict):
            smin = to_float(first(val.get("minValue"), val.get("value")))
            smax = to_float(first(val.get("maxValue"), val.get("value")))
            period = salary_period(val.get("unitText"))
        else:
            smin = smax = to_float(val)
        period = period or salary_period(amount.get("unitText"))
    if smin is None and smax is None:
        currency = period = None
    elif not currency:
        currency = default_currency

    employment = [str(e) for e in as_list(jp.get("employmentType")) if e]
    description = html_to_text(jp.get("description"))
    title = html_lib.unescape(str(jp.get("title") or "")).strip()
    tags = _skills(jp.get("skills")) + contract_tags(title, description, employment_types=employment)
    valid = parse_date(jp.get("validThrough"))
    posted = parse_date(_compact_date(jp.get("datePosted")))
    return Posting(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=html_lib.unescape(str(company or "")).strip(),
        url=url,
        apply_url=None,
        location_raw="; ".join(labels),
        countries=countries,
        cities=cities,
        workplace=workplace,
        remote_scope=remote_scope,
        description=description,
        posted_at=posted,
        employment_types=employment,
        salary_min=smin,
        salary_max=smax,
        salary_currency=currency,
        salary_period=period,
        tags=list(dict.fromkeys(tags)),
        raw={
            "validThrough": valid.isoformat() if valid else None,
            "industry": first(jp.get("industry"), jp.get("occupationalCategory")),
            "experience": jp.get("experienceRequirements"),
            "identifier": jp.get("identifier"),
        },
    )


# ------------------------------------------------------------------ URL helpers
def _last_segment(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    return unquote(path.rsplit("/", 1)[-1])


def _numeric_id(job_id: str) -> int:
    m = re.search(r"\d+", job_id)
    return int(m.group(0)) if m else 0


def _rx_group(rx: re.Pattern[str] | None, url: str, group: str) -> str | None:
    if rx is None:
        return None
    m = rx.search(url)
    return m.group(group) if m and m.groupdict().get(group) else None


def slug_text(slug: str, strip: list[re.Pattern[str]]) -> str:
    for rx in strip:
        slug = rx.sub("", slug)
    return re.sub(r"[-_+]+", " ", slug).strip()


# ------------------------------------------------------------------ adapter
@adapter("sitemap_jsonld")
def sitemap_jsonld(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    include = [re.compile(x, re.I) for x in as_list(p.get("url_include"))]
    exclude = [re.compile(x, re.I) for x in as_list(p.get("url_exclude"))]
    id_rx = re.compile(p["id_regex"]) if p.get("id_regex") else None
    slug_rx = re.compile(p["slug_regex"]) if p.get("slug_regex") else None
    strip = [re.compile(x, re.I) for x in as_list(p.get("slug_strip"))]
    group_rx = re.compile(p["group_strip"], re.I) if p.get("group_strip") else None
    prefer = [re.compile(x, re.I) for x in as_list(p.get("prefer"))]
    newest_id_first = bool(p.get("newest_id_first", False))
    max_details = int(p.get("max_details", 40))
    lastmod_days = p.get("lastmod_days")
    title_gate = bool(p.get("title_gate", True))
    skip_known = bool(p.get("skip_known", True))
    skip_expired = bool(p.get("skip_expired", True))
    enrich = _get_enricher(p.get("enrich"))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=float(lastmod_days)) if lastmod_days else None

    entries, docs = collect_sitemap_urls(ctx) if (p.get("sitemap_index") or p.get("sitemaps")) else ([], 0)
    if p.get("listing_urls"):
        listed, pages = collect_listing_urls(ctx)
        known_locs = {e.loc for e in entries}
        entries += [e for e in listed if e.loc not in known_locs]
        docs += pages
    stats = {"sitemap_urls": len(entries), "sitemap_docs": docs, "url_filter": 0, "lastmod": 0, "known": 0,
             "title_gate": 0, "groups": 0, "fetched": 0, "parsed": 0, "no_jsonld": 0, "gone": 0, "expired": 0,
             "covered": 0, "robots": 0}

    def job_id(url: str) -> str:
        return _rx_group(id_rx, url, "id") or _last_segment(url)

    def slug_of(url: str) -> str:
        return _rx_group(slug_rx, url, "slug") or _last_segment(url)

    # -- pre-filter (no network) ------------------------------------------------
    groups: dict[str, list[SitemapEntry]] = {}
    known_groups: set[str] = set()
    for e in entries:
        if include and not any(rx.search(e.loc) for rx in include):
            continue
        if any(rx.search(e.loc) for rx in exclude):
            continue
        stats["url_filter"] += 1
        if cutoff and e.lastmod and e.lastmod < cutoff:
            continue
        stats["lastmod"] += 1
        slug = slug_of(e.loc)
        gkey = group_rx.sub("", slug) if group_rx else slug
        if skip_known and ctx.is_known(ctx.spec.id, job_id(e.loc)):
            ctx.mark_seen(ctx.spec.id, job_id(e.loc))
            stats["known"] += 1
            known_groups.add(gkey)
            continue
        if title_gate and not ctx.wants_detail(slug_text(slug, strip)):
            ctx.skipped += 1
            continue
        stats["title_gate"] += 1
        groups.setdefault(gkey, []).append(e)
    for g in known_groups:
        groups.pop(g, None)
    stats["groups"] = len(groups)

    def rank(item: tuple[str, list[SitemapEntry]]) -> tuple[int, float]:
        e = item[1][0]
        pref = next((i for i, rx in enumerate(prefer) if rx.search(e.loc)), len(prefer))
        if newest_id_first:
            newest = float(max((_numeric_id(job_id(x.loc)) for x in item[1]), default=0))
        else:
            newest = max((x.lastmod.timestamp() for x in item[1] if x.lastmod), default=0.0)
        return pref, -newest

    ordered = sorted(groups.items(), key=rank)
    covered: set[str] = set()
    for _gkey, members in ordered:
        if stats["fetched"] >= max_details:
            break
        if ctx.http.remaining <= 0:
            ctx.notes.append("request budget exhausted before max_details")
            break
        members = sorted(members, key=lambda m: m.lastmod.timestamp() if m.lastmod else 0.0, reverse=True)
        e = next((m for m in members if m.loc not in covered), None)
        if e is None:
            stats["covered"] += 1
            continue
        try:
            resp = ctx.http.get(e.loc)
        except Blocked as exc:
            if exc.reason.startswith("robots"):
                stats["robots"] += 1
                covered.update(m.loc for m in members)
                continue
            ctx.notes.append(_summary(stats))
            raise
        stats["fetched"] += 1
        if resp.status_code in (404, 410):
            stats["gone"] += 1
            continue
        if resp.status_code != 200:
            ctx.notes.append(f"{e.loc}: HTTP {resp.status_code}")
            continue
        page = resp.text
        jp = extract_jobposting(page)
        if not jp:
            stats["no_jsonld"] += 1
            continue
        post = jobposting_to_posting(jp, source=ctx.spec.id, source_job_id=job_id(e.loc), url=e.loc,
                                     default_currency=p.get("default_currency"),
                                     default_company=p.get("default_company"))
        if enrich:
            try:
                covered.update(enrich(post, page, ctx) or [])
            except Exception as exc:  # noqa: BLE001 - enrichment is best-effort, JSON-LD is enough
                ctx.notes.append(f"enrich failed for {e.loc}: {type(exc).__name__}: {exc}"[:200])
        covered.update(m.loc for m in members)
        valid = parse_date((post.raw or {}).get("validThrough"))
        if skip_expired and valid and valid < now:
            stats["expired"] += 1
            continue
        if post.raw is not None:
            post.raw["lastmod"] = e.lastmod.isoformat() if e.lastmod else None
        stats["parsed"] += 1
        yield post
    ctx.notes.append(_summary(stats))


def _summary(stats: dict[str, int]) -> str:
    return (
        f"sitemap_urls={stats['sitemap_urls']} (docs={stats['sitemap_docs']}) url_filter={stats['url_filter']} "
        f"lastmod={stats['lastmod']} known={stats['known']} title_gate={stats['title_gate']} "
        f"groups={stats['groups']} fetched={stats['fetched']} parsed={stats['parsed']} "
        f"no_jsonld={stats['no_jsonld']} gone={stats['gone']} expired={stats['expired']} robots={stats['robots']}"
    )
