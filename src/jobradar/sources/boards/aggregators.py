"""Job-search aggregators: Adzuna (key), Jooble (key), python-jobspy/Indeed (optional dep),
and the ConorsCode open-jobs-data daily dataset.

Keys come from ``ctx.secrets`` only and are never written to notes, raw or error
messages (request URLs that embed a key are redacted before raising).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.parse import quote, urlencode

from jobradar.http.client import Blocked
from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, default_terms, as_list, first, parse_date, to_float
from jobradar.sources.boards.national_apis import country_iso


class SourceError(RuntimeError):
    """Adapter-level failure whose message is safe to log (no secrets)."""


def _redact(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def _send(ctx: FetchContext, method: str, url: str, secrets: tuple[str, ...], **kw: Any) -> Any:
    """Request JSON; on refusal/HTTP error raise with the key redacted from the message."""
    try:
        resp = ctx.http.get(url, **kw) if method == "GET" else ctx.http.post(url, **kw)
    except Blocked as exc:
        raise Blocked(_redact(exc.url, *secrets), _redact(exc.reason, *secrets), exc.status) from None
    if resp.status_code >= 400:
        body = _redact(resp.text[:200], *secrets)
        raise SourceError(f"HTTP {resp.status_code}: {body}")
    try:
        return resp.json()
    except ValueError:
        raise SourceError(f"non-JSON response ({resp.headers.get('content-type')})") from None


# ==================================================================== Adzuna
ADZUNA_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
ADZUNA_COUNTRIES = ["pl", "de", "nl", "at", "be", "ch", "es", "fr", "it", "gb"]


@adapter("adzuna")
def adzuna(ctx: FetchContext) -> Iterator[Posting]:
    """Adzuna search API (developer.adzuna.com; free key: ADZUNA_APP_ID + ADZUNA_APP_KEY).

    GET /v1/api/jobs/{country}/search/{page}?app_id&app_key&what_phrase|what&max_days_old
    &results_per_page<=50&sort_by=date. Descriptions are ~500-char snippets; ``redirect_url``
    must be used as the link (Adzuna attribution terms). Calls per run =
    countries × queries × pages, hard-capped by ``max_calls`` (default 60 ≤ 250/day budget).
    """
    p = ctx.params
    app_id = (ctx.secrets or {}).get("ADZUNA_APP_ID") or ""
    app_key = (ctx.secrets or {}).get("ADZUNA_APP_KEY") or ""
    if not (app_id and app_key):
        raise SourceError("ADZUNA_APP_ID / ADZUNA_APP_KEY missing")
    countries = [c.lower() for c in (p.get("countries") or ADZUNA_COUNTRIES) if c.lower() in ADZUNA_COUNTRIES]
    queries = p.get("queries") or default_terms(4)
    per_page = min(int(p.get("results_per_page", 50)), 50)
    max_pages = int(p.get("max_pages", 1))
    max_days = int(p.get("max_days_old", 3))
    max_calls = int(p.get("max_calls", 60))
    phrase = bool(p.get("phrase", True))
    category = p.get("category")  # e.g. "it-jobs"
    calls = 0
    seen: set[str] = set()
    for country in countries:
        iso = "GB" if country == "gb" else country.upper()
        for q in queries:
            for page in range(1, max_pages + 1):
                if calls >= max_calls:
                    ctx.notes.append(f"max_calls {max_calls} reached")
                    return
                params = {"app_id": app_id, "app_key": app_key, "results_per_page": per_page,
                          "max_days_old": max_days, "sort_by": "date",
                          ("what_phrase" if phrase else "what"): q, "category": category}
                url = f"{ADZUNA_URL.format(country=country, page=page)}?{urlencode({k: v for k, v in params.items() if v not in (None, '')}, quote_via=quote)}"
                data = _send(ctx, "GET", url, (app_id, app_key))
                calls += 1
                rows = data.get("results") or []
                for j in rows:
                    jid = f"{country}:{j.get('id')}"
                    if not j.get("id") or jid in seen:
                        continue
                    seen.add(jid)
                    loc = j.get("location") or {}
                    area = [a for a in as_list(loc.get("area")) if a]
                    contract = [x for x in (j.get("contract_time"), j.get("contract_type")) if x]
                    predicted = str(j.get("salary_is_predicted")) == "1"
                    sal_min = None if predicted else to_float(j.get("salary_min"))
                    sal_max = None if predicted else to_float(j.get("salary_max"))
                    yield Posting(
                        source=ctx.spec.id,
                        source_job_id=jid,
                        title=html_to_text(j.get("title")),
                        company=(j.get("company") or {}).get("display_name") or "",
                        url=j.get("redirect_url") or "",
                        location_raw=loc.get("display_name") or ", ".join(area),
                        countries=[iso],  # the country endpoint is the structured location
                        cities=[area[-1]] if len(area) >= 3 else [],
                        description=html_to_text(j.get("description")),
                        posted_at=parse_date(j.get("created")),
                        employment_types=contract,
                        salary_min=sal_min,
                        salary_max=sal_max,
                        salary_currency=None,  # Adzuna returns local-currency numbers without a code
                        salary_period="year" if (sal_min or sal_max) else None,
                        tags=[x for x in ((j.get("category") or {}).get("label"),) if x],
                        raw={"adref": j.get("adref"), "category": (j.get("category") or {}).get("tag"),
                             "salary_is_predicted": j.get("salary_is_predicted"), "area": area},
                    )
                if len(rows) < per_page:
                    break


# ==================================================================== Jooble
JOOBLE_URL = "https://{host}/api/{key}"
JOOBLE_LOCATIONS = ["Poland", "Remote"]


@adapter("jooble")
def jooble(ctx: FetchContext) -> Iterator[Posting]:
    """Jooble REST API (free key on request). POST /api/{key} with JSON
    {keywords, location, page, ResultOnPage, datecreatedfrom}. Returns snippets plus a
    jooble.org redirect link; ``location`` is free text, so no structured country.

    A key is issued for one Jooble country site and is refused (403) on any other: the
    default host ``jooble.org`` searches the US index. Each country site is its own source
    with ``host`` set and its key named first in ``secrets`` (e.g. JOOBLE_DE_API_KEY).
    An empty ``location`` searches the whole country site.

    ``remote_only``: keep only results with a remote word in title, location or snippet (the
    location "Remote" maps to the site's own Teletrabajo/Home office/Homeoffice/Thuiswerken;
    probed 2026-10-31)."""
    from jobradar.normalize.location import mentions_remote

    p = ctx.params
    remote_only = bool(p.get("remote_only"))
    key_name = (ctx.spec.secrets or ["JOOBLE_PL_API_KEY"])[0]
    key = (ctx.secrets or {}).get(key_name) or ""
    if not key:
        raise SourceError(f"{key_name} missing")
    keywords = p.get("keywords") or default_terms(4)
    locations = p.get("locations") or JOOBLE_LOCATIONS
    per_page = int(p.get("per_page", 50))
    max_pages = int(p.get("max_pages", 1))
    days = p.get("published_within_days", 3)
    max_calls = int(p.get("max_calls", 20))
    since = (datetime.now(timezone.utc) - timedelta(days=float(days))).strftime("%Y-%m-%d") if days else None
    url = JOOBLE_URL.format(host=p.get("host", "jooble.org"), key=key)
    calls = 0
    seen: set[str] = set()
    for loc in locations:
        for kw in keywords:
            for page in range(1, max_pages + 1):
                if calls >= max_calls:
                    ctx.notes.append(f"max_calls {max_calls} reached")
                    return
                body = {"keywords": kw, "location": loc, "page": str(page), "ResultOnPage": str(per_page)}
                if since:
                    body["datecreatedfrom"] = since
                data = _send(ctx, "POST", url, (key,), json=body)
                calls += 1
                jobs = data.get("jobs") or []
                for j in jobs:
                    jid = str(first(j.get("id"), j.get("link")) or "")
                    if not jid or jid in seen:
                        continue
                    seen.add(jid)
                    if remote_only and not mentions_remote(" | ".join(
                            [html_to_text(j.get("title")), j.get("location") or "", html_to_text(j.get("snippet"))])):
                        ctx.skipped += 1
                        continue
                    yield Posting(
                        source=ctx.spec.id,
                        source_job_id=jid,
                        title=html_to_text(j.get("title")),
                        company=j.get("company") or "",
                        url=j.get("link") or "",
                        location_raw=j.get("location") or "",
                        description=html_to_text(j.get("snippet")),
                        posted_at=parse_date(j.get("updated")),
                        employment_types=as_list(j.get("type") or None),
                        raw={"salary": j.get("salary") or None, "source": j.get("source"),
                             "query": {"keywords": kw, "location": loc}},
                    )
                if len(jobs) < per_page:
                    break


# ======================================================== open-jobs-data (ConorsCode)
OJD_BASE = "https://raw.githubusercontent.com/ConorsCode/open-jobs-data/main/data/"
_ATS_SLUG = [
    ("greenhouse", re.compile(r"greenhouse\.io/(?:embed/job_app\?for=|v1/boards/)?([A-Za-z0-9_.-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([^/?#]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([^/?#]+)", re.I)),
    ("recruitee", re.compile(r"https?://([^./]+)\.recruitee\.com", re.I)),
    ("personio", re.compile(r"https?://([^./]+)\.jobs\.personio\.(?:de|com)", re.I)),
    ("bamboohr", re.compile(r"https?://([^./]+)\.bamboohr\.com", re.I)),
    ("workday", re.compile(r"https?://([^./]+)\.wd\d+\.myworkdayjobs\.com", re.I)),  # slug = tenant
]


def ats_slug_from_url(platform: str | None, url: str) -> str | None:
    for name, rx in _ATS_SLUG:
        if platform and name != platform:
            continue
        m = rx.search(url or "")
        if m:
            slug = m.group(1)
            return None if slug in ("jobs", "boards", "embed") else slug
    return None


@adapter("open_jobs_data")
def open_jobs_data(ctx: FetchContext) -> Iterator[Posting]:
    """ConorsCode/open-jobs-data: a daily GitHub-Actions snapshot of ~38k postings from
    ~9 ATS platforms (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, ...).

    ``file``: new-jobs.json (default, ~1 MB: postings not present in the previous daily
    snapshot — run daily or you miss days) or jobs.json (~18 MB full snapshot, e.g. weekly).
    No descriptions in the dataset (title/locations/isRemote/applyUrl/postedAt only).
    """
    p = ctx.params
    fname = p.get("file", "new-jobs.json")
    title_only = bool(p.get("title_prefilter", False))
    resp = ctx.http.get(OJD_BASE + fname)
    if resp.status_code != 200:
        raise SourceError(f"{fname}: HTTP {resp.status_code}")
    rows = resp.json()
    if not isinstance(rows, list):
        raise SourceError(f"{fname}: unexpected shape {type(rows).__name__}")
    if fname.startswith("new") and not rows:
        ctx.notes.append("new-jobs.json empty (first run upstream or no new jobs)")
    slug_by_company: dict[tuple[str, str], str] = {}
    if p.get("resolve_slugs", True) and any(
            not ats_slug_from_url((j.get("platform") or "").lower(), j.get("applyUrl") or "") for j in rows[:5000]):
        # custom career domains (stripe.com/jobs?gh_jid=) → slug from the dataset's companies.json
        try:
            comp = ctx.http.get_json(OJD_BASE.replace("/data/", "/") + "companies.json")
            items = comp if isinstance(comp, list) else (comp or {}).get("companies", [])
            for c in items:
                plat = (c.get("platform") or "").lower()
                slug = (c.get("workday") or {}).get("tenant") if plat == "workday" else c.get("slug")
                if plat and slug and c.get("name"):
                    slug_by_company[(plat, c["name"])] = str(slug)
        except Exception as exc:  # noqa: BLE001 - slugs are a nice-to-have
            ctx.notes.append(f"companies.json: {type(exc).__name__}")
    for j in rows:
        platform = (j.get("platform") or "").lower() or None
        job_id = str(j.get("jobId") or "")
        title = (j.get("title") or "").strip()
        if not job_id or not title:
            continue
        if title_only and not ctx.wants_detail(title):
            continue
        apply_url = j.get("applyUrl") or ""
        locs = [str(x) for x in as_list(j.get("locations")) if x]
        yield Posting(
            source=ctx.spec.id,
            source_job_id=f"{platform}:{job_id}",
            title=title,
            company=j.get("company") or "",
            url=apply_url,
            apply_url=apply_url or None,
            location_raw=" | ".join(locs),
            workplace="remote" if j.get("isRemote") is True else None,
            posted_at=parse_date(first(j.get("postedAt"), j.get("firstSeenAt"))),
            employment_types=as_list(j.get("employmentType") or None),
            tags=[j["department"]] if j.get("department") else [],
            ats_type=platform,
            ats_slug=ats_slug_from_url(platform, apply_url) or slug_by_company.get((platform or "", j.get("company") or "")),
            ats_job_id=job_id,
            raw={"firstSeenAt": j.get("firstSeenAt"), "scrapedAt": j.get("scrapedAt"),
                 "postedAt": j.get("postedAt")},
        )
