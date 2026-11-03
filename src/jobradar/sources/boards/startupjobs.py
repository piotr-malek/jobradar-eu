"""StartupJobs.cz (Czechia/Slovakia startups) via the API Platform backend its Nuxt frontend calls.

robots.txt (www) allows everything but /admin/; the API lives on back.startupjobs.cz.

List:   POST https://back.startupjobs.cz/api/search-offers?page=N  (Content-Type: application/ld+json)
        body {"query": "<text>", "locationPreferences": [...]} → Hydra collection, 20 per page
        (``member``: id, name{cs,en}, slug, seniorities, hours, contracts, locationPreferences
        [remote|hybrid|onsite], locations[{name{cs,en}}], company{name,slug}, salary), ``view.next``.
Detail: GET https://back.startupjobs.cz/api/offers/<id> → description, dates (for titles passing
        ``ctx.wants_detail`` that are not stored yet; ``max_details``).

Params: queries (default: the role packs' search terms), location_preferences [] (all),
        max_pages 3, max_details 20, lang en.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from jobradar.http.client import Blocked, BudgetExceeded
from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, default_terms, as_list, first, parse_date, to_float
from jobradar.sources.generic.sitemap_jsonld import country_iso2

API = "https://back.startupjobs.cz"
SITE = "https://www.startupjobs.cz"
HEADERS = {"Content-Type": "application/ld+json", "Accept": "application/ld+json"}
FLEX = ("remote", "hybrid", "onsite")


def _tr(value: Any, lang: str) -> str:
    if isinstance(value, dict):
        return str(first(value.get(lang), value.get("en"), value.get("cs"), *value.values()) or "")
    return str(value or "")


def offer_to_posting(source: str, o: dict[str, Any], lang: str = "en") -> Posting | None:
    oid = o.get("id")
    title = _tr(o.get("name"), lang).strip()
    if not oid or not title:
        return None
    slug = _tr(o.get("slug"), "cs") or "nabidka"
    labels, countries, cities = [], [], []
    for loc in as_list(o.get("locations")):
        if not isinstance(loc, dict):
            continue
        label = _tr(loc.get("name"), "en")
        if label:
            labels.append(label)
            iso = country_iso2(label.rsplit(",", 1)[-1].strip()) if "," in label else None
            if iso and iso not in countries:
                countries.append(iso)
        place = _tr(loc.get("place"), "en")
        if place and place not in cities:
            cities.append(place)
    prefs = [p for p in as_list(o.get("locationPreferences")) if p in FLEX]
    workplace = next((f for f in FLEX if f in prefs), None)  # most flexible option offered
    company = o.get("company") or {}
    sal = o.get("salary") if isinstance(o.get("salary"), dict) else {}
    smin, smax = to_float(sal.get("min")), to_float(sal.get("max"))
    period = {"month": "month", "monthly": "month", "year": "year", "yearly": "year", "hour": "hour",
              "hourly": "hour", "day": "day"}.get(str(sal.get("period") or sal.get("measure") or "").lower())
    return Posting(
        source=source,
        source_job_id=str(oid),
        title=title,
        company=str(company.get("name") or "").strip(),
        url=f"{SITE}/nabidka/{oid}/{slug}",
        location_raw="; ".join(labels) or ("Remote" if workplace == "remote" else ""),
        countries=countries,
        cities=cities,
        workplace=workplace,
        employment_types=[str(x) for x in as_list(o.get("hours")) + as_list(o.get("contracts"))],
        salary_min=smin,
        salary_max=smax,
        salary_currency=sal.get("currency") if (smin or smax) else None,
        salary_period=period if (smin or smax) else None,
        tags=[f"seniority:{s}" for s in as_list(o.get("seniorities"))] + [f"mode:{p}" for p in prefs],
        raw={"locationPreferences": prefs, "startup": company.get("startup"), "company_slug": company.get("slug")},
    )


def merge_offer_detail(post: Posting, d: dict[str, Any], lang: str = "en") -> None:
    desc = first(d.get("description"), d.get("content"), d.get("text"))
    parts = [html_to_text(_tr(desc, lang))] if desc else []
    for key in ("requirements", "benefitsText", "offer", "responsibilities"):
        if d.get(key):
            parts.append(html_to_text(_tr(d.get(key), lang)))
    langs = []
    for req in as_list(d.get("requiredLanguages")):
        if isinstance(req, dict) and isinstance(req.get("language"), dict):
            name = _tr(req["language"].get("name"), "en")
            langs.append(f"{name} ({req.get('level')})" if req.get("level") else name)
    if langs:  # structured requirement → plain text the matcher's language check understands
        parts.append("Required languages: " + ", ".join(langs))
    text = "\n\n".join(x for x in parts if x)
    if text:
        post.description = text
    for req in as_list(d.get("requiredSkills")):
        skill = req.get("skill") if isinstance(req, dict) else None
        if isinstance(skill, dict) and _tr(skill.get("name"), "en"):
            post.tags.append(_tr(skill.get("name"), "en"))
    post.posted_at = parse_date(first(d.get("publishedAt"), d.get("activatedAt"), d.get("createdAt"),
                                      d.get("updatedAt")))
    link = first(d.get("externalUrl"), d.get("applyUrl"), d.get("url"))
    if isinstance(link, str) and link.startswith("http"):
        post.apply_url = link


@adapter("startupjobs_cz")
def startupjobs_cz(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    lang = p.get("lang", "en")
    max_details = int(p.get("max_details", 20))
    seen: set[str] = set()
    to_detail: list[Posting] = []
    stop = False
    for query in as_list(p.get("queries") or default_terms(3)):
        body: dict[str, Any] = {"query": query} if query else {}
        if p.get("location_preferences"):
            body["locationPreferences"] = as_list(p["location_preferences"])
        for page in range(1, int(p.get("max_pages", 3)) + 1):
            try:
                data = ctx.http.post_json(f"{API}/api/search-offers?page={page}", headers=HEADERS,
                                          content=json.dumps(body))
            except (Blocked, BudgetExceeded) as exc:
                ctx.notes.append(f"search stopped: {exc}")
                stop = True
                break
            members = data.get("member") or data.get("hydra:member") or []
            for o in members:
                post = offer_to_posting(ctx.spec.id, o, lang)
                if not post or post.source_job_id in seen:
                    continue
                seen.add(post.source_job_id)
                if not ctx.wants_detail(post.title):
                    yield post
                elif not ctx.is_known(ctx.spec.id, post.source_job_id):
                    to_detail.append(post)
            view = data.get("view") or data.get("hydra:view") or {}
            if not members or not (view.get("next") or view.get("hydra:next")):
                break
        if stop:
            break
    blocked = False
    for i, post in enumerate(to_detail):
        if not blocked and i < max_details:
            try:
                resp = ctx.http.get(f"{API}/api/offers/{post.source_job_id}", headers={"Accept": "application/ld+json"})
                if resp.status_code in (404, 410):
                    continue
                if resp.status_code == 200:
                    merge_offer_detail(post, resp.json(), lang)
                else:
                    ctx.notes.append(f"detail {post.source_job_id}: HTTP {resp.status_code}")
            except (Blocked, BudgetExceeded) as exc:
                blocked = True
                ctx.notes.append(f"detail stopped: {exc}")
            except ValueError:
                ctx.notes.append(f"detail {post.source_job_id}: bad JSON")
        yield post
