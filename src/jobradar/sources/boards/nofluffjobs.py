"""NoFluffJobs: server-rendered listing pages → (title gate) → offer pages' JSON-LD.

robots.txt disallows ``/api/`` and ``/posting/`` and publishes no sitemap, but allows the public
listing pages (``/?criteria=…``) and offer pages (``/job/<slug>``). The Angular server render embeds the
search result in ``<script id="serverApp-state">`` (20 postings per page; ``page=N`` returns the first
N×20, capped at 100 by the server, so one request covers 5 pages). Order is by last bump ("renewed"),
not publication: one category's top 100 spans roughly the last day of bumps, and offers are re-bumped
every few days, so a daily run per category sees nearly every active offer within a week. Each posting has title, company, work mode (remote / hybrid /
onsite), cities, salary + contract type and posted date — enough for the matcher's title/location
stage. Descriptions come from the offer page's JSON-LD JobPosting, fetched only for postings that pass
``ctx.wants_detail(title)`` and are not stored yet (``max_details`` per run).

Params:
  listings: [url]   listing URLs; ``{page}`` is replaced by ``pages`` (default: data, BI, AI categories)
  pages: int (5)    cumulative page number → up to pages×20 (server max 100) postings per request
  max_details: int (30)
  list_only: bool   never fetch offer pages (no descriptions)
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

from jobradar.models import Posting
from jobradar.sources.base import FetchContext, adapter, as_list, first, parse_date, to_float
from jobradar.sources.generic.sitemap_jsonld import (
    contract_tags,
    country_iso2,
    extract_jobposting,
    jobposting_to_posting,
    salary_period,
)

BASE = "https://nofluffjobs.com"


def default_listings() -> list[str]:
    """One listing per NoFluffJobs category of the active role packs."""
    from jobradar import config

    return [f"{BASE}/?criteria=category%3D{cat}&page={{page}}" for cat in config.board_map("nofluffjobs").get("categories", [])]


_STATE = re.compile(r"<script[^>]+id=[\"']serverApp-state[\"'][^>]*>(.*?)</script>", re.S)
_MODE = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "office": "onsite"}
_CONTRACT = {"b2b": "b2b", "permanent": "uop", "zlecenie": "uz", "mandate": "uz", "uod": "uod"}


def server_state(page: str) -> dict[str, Any]:
    m = _STATE.search(page)
    if not m:
        return {}
    raw = m.group(1)
    try:
        return json.loads(raw)
    except ValueError:  # older Angular TransferState escaping
        raw = raw.replace("&q;", '"').replace("&a;", "&").replace("&s;", "'").replace("&l;", "<").replace("&g;", ">")
        try:
            return json.loads(raw)
        except ValueError:
            return {}


def listing_postings(state: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
    for value in state.values():
        if isinstance(value, dict) and "criteriaSearch" in value and isinstance(value.get("postings"), list):
            return value["postings"], value.get("totalCount")
    return [], None


def _places(location: dict[str, Any]) -> tuple[list[str], list[str]]:
    cities: list[str] = []
    countries: list[str] = []
    for pl in (location or {}).get("places") or []:
        if not isinstance(pl, dict):
            continue
        iso = country_iso2((pl.get("country") or {}).get("code")) or country_iso2((pl.get("country") or {}).get("name"))
        if iso and iso not in countries:
            countries.append(iso)
        city = pl.get("city")
        if city and city.lower() != "remote" and pl.get("country") and city not in cities:
            cities.append(city)
    return cities, countries


def list_posting(ctx: FetchContext, j: dict[str, Any]) -> Posting:
    loc = j.get("location") or {}
    cities, countries = _places(loc)
    mode = _MODE.get(str(loc.get("type") or "").lower())
    if j.get("fullyRemote") or loc.get("fullyRemote"):
        mode = "remote"
    sal = j.get("salary") or {}
    contract = _CONTRACT.get(str(sal.get("type") or "").lower())
    shown = cities[:8]
    location_raw = ", ".join(shown) + (f" (+{len(cities) - 8})" if len(cities) > 8 else "")
    if mode == "remote":
        location_raw = "Remote" + (f"; {location_raw}" if location_raw else "")
    tiles = [t.get("value") for t in (j.get("tiles") or {}).get("values") or []
             if isinstance(t, dict) and t.get("type") == "requirement" and t.get("value")]
    has_salary = to_float(sal.get("from")) or to_float(sal.get("to"))
    slug = j.get("url") or j.get("id")
    return Posting(
        source=ctx.spec.id,
        source_job_id=str(first(j.get("reference"), j.get("id"), slug)),
        title=(j.get("title") or "").strip(),
        company=(j.get("name") or "").strip(),
        url=f"{BASE}/job/{slug}",
        location_raw=location_raw,
        countries=countries,
        cities=cities,
        workplace=mode,
        remote_scope=", ".join(countries) if mode == "remote" and countries else None,
        posted_at=parse_date(j.get("posted")),
        salary_min=to_float(sal.get("from")),
        salary_max=to_float(sal.get("to")),
        salary_currency=sal.get("currency") if has_salary else None,
        salary_period=salary_period(sal.get("period")) if has_salary else None,
        employment_types=[str(sal["type"])] if sal.get("type") else [],
        tags=list(dict.fromkeys(tiles + ([contract] if contract else []))),
        raw={"slug": slug, "category": j.get("category"), "seniority": j.get("seniority"),
             "renewed": j.get("renewed"), "regions": j.get("regions")},
    )


def merge_detail(post: Posting, page: str) -> bool:
    """Fill description etc. from the offer page; returns False when the page has no JobPosting."""
    jp = extract_jobposting(page)
    if not jp:
        return False
    d = jobposting_to_posting(jp, source=post.source, source_job_id=post.source_job_id, url=post.url)
    post.description = d.description
    post.workplace = post.workplace or d.workplace
    post.employment_types = list(dict.fromkeys(post.employment_types + d.employment_types))
    if post.salary_min is None and post.salary_max is None and (d.salary_min or d.salary_max):
        post.salary_min, post.salary_max = d.salary_min, d.salary_max
        post.salary_currency, post.salary_period = d.salary_currency, d.salary_period
    post.posted_at = post.posted_at or d.posted_at
    extra: list[str] = []
    state = server_state(page)
    detail = next((v for k, v in state.items() if k.startswith("/posting/") and isinstance(v, dict)), None)
    if detail:
        types = (((detail.get("essentials") or {}).get("originalSalary") or {}).get("types") or {})
        extra += [_CONTRACT[t] for t in types if t in _CONTRACT]
        if detail.get("expiresAt"):
            post.raw = {**(post.raw or {}), "expiresAt": detail.get("expiresAt")}
    post.tags = list(dict.fromkeys(post.tags + extra + d.tags + contract_tags(post.description)))
    return True


@adapter("nofluffjobs")
def nofluffjobs(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    pages = int(p.get("pages", 5))
    max_details = 0 if p.get("list_only") else int(p.get("max_details", 30))
    seen: set[str] = set()
    to_detail: list[Posting] = []
    total = gated = known = 0
    for listing in as_list(p.get("listings")) or default_listings():
        resp = ctx.http.get(listing.replace("{page}", str(pages)))
        if resp.status_code != 200:
            ctx.notes.append(f"listing HTTP {resp.status_code}: {listing}")
            continue
        rows, count = listing_postings(server_state(resp.text))
        if not rows:
            ctx.notes.append(f"no serverApp-state postings in {listing}")
        ctx.notes.append(f"listing total={count} returned={len(rows)}")
        for j in rows:
            post = list_posting(ctx, j)
            if not post.title or post.source_job_id in seen:
                continue
            seen.add(post.source_job_id)
            total += 1
            if not ctx.wants_detail(post.title):
                yield post  # title-excluded: list-level row is enough (drop audit)
                continue
            gated += 1
            if ctx.is_known(ctx.spec.id, post.source_job_id):
                ctx.mark_seen(ctx.spec.id, post.source_job_id)
                known += 1  # already stored with description; re-yielding list-level would churn versions
                continue
            to_detail.append(post)
    to_detail.sort(key=lambda x: x.posted_at.timestamp() if x.posted_at else 0, reverse=True)
    fetched = parsed = 0
    for post in to_detail:
        if fetched >= max_details or ctx.http.remaining <= 0:
            break
        resp = ctx.http.get(post.url)
        fetched += 1
        if resp.status_code in (404, 410):
            continue
        if resp.status_code == 200 and merge_detail(post, resp.text):
            parsed += 1
        yield post
    if p.get("list_only"):
        yield from to_detail
    ctx.notes.append(f"postings={total} title_gate={gated} known={known} details_fetched={fetched} "
                     f"parsed={parsed} deferred={max(0, len(to_detail) - fetched) if not p.get('list_only') else 0}")
