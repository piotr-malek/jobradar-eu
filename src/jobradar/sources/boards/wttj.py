"""Welcome to the Jungle (welcometothejungle.com) via the public Algolia search index its frontend uses.

1. GET https://www.welcometothejungle.com/api/env (runtime config the page loads as a <script>;
   robots.txt allows it) → ``PUBLIC_ALGOLIA_APPLICATION_ID`` + ``PUBLIC_ALGOLIA_API_KEY_CLIENT``
   (search-only key, public by design). Read on every run so key rotations need no code change;
   ``algolia_app`` / ``algolia_key`` params override.
2. POST https://<app>-dsn.algolia.net/1/indexes/<index>/query with the site as Referer/Origin.
   Index ``wttj_jobs_production_en`` holds every job (all languages); ``language`` is a facet.

Hit → Posting: offices[].country_code (ISO-2) → countries; ``remote`` fulltime → remote,
partial → hybrid, no → onsite (punctual/unknown left empty, kept as tag); summary + key missions +
profile → description; salary_minimum/maximum/currency/period; contract_type → employment_types.

Params: queries (default: the role packs' search terms), languages [en], remote [fulltime, partial],
        exclude_countries [US, CA], exclude_contracts [internship, apprenticeship, ...],
        hits_per_page 100, max_pages 2, index, extra_filters.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

from jobradar.http.client import Blocked, BudgetExceeded
from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, default_terms, as_list, first, parse_date, to_float

SITE = "https://www.welcometothejungle.com"
DEFAULT_INDEX = "wttj_jobs_production_en"
ATTRS = [
    "objectID", "reference", "wk_reference", "name", "slug", "organization.name", "organization.slug",
    "offices", "remote", "contract_type", "salary_minimum", "salary_maximum", "salary_currency",
    "salary_period", "published_at", "summary", "key_missions", "profile", "language", "new_profession",
    "experience_level_minimum", "has_remote",
]
REMOTE_MAP = {"fulltime": "remote", "partial": "hybrid", "no": "onsite"}
PERIOD_MAP = {"yearly": "year", "monthly": "month", "daily": "day", "hourly": "hour", "weekly": None}


def read_env(text: str) -> dict[str, str]:
    m = re.search(r"window\.env\s*=\s*(\{.*?\})\s*;?\s*$", text.strip(), re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except ValueError:
        return {}


def build_filters(p: dict[str, Any]) -> str:
    parts: list[str] = []
    langs = as_list(p.get("languages", ["en"]))
    if langs:
        parts.append("(" + " OR ".join(f"language:{x}" for x in langs) + ")")
    remote = as_list(p.get("remote", ["fulltime", "partial"]))
    if remote:
        parts.append("(" + " OR ".join(f"remote:{x}" for x in remote) + ")")
    for cc in as_list(p.get("exclude_countries", ["US", "CA"])):
        parts.append(f"NOT offices.country_code:{cc}")
    for ct in as_list(p.get("exclude_contracts", ["internship", "apprenticeship", "vie", "graduate_program"])):
        parts.append(f"NOT contract_type:{ct}")
    if p.get("extra_filters"):
        parts.append(f"({p['extra_filters']})")
    return " AND ".join(parts)


def hit_to_posting(source: str, h: dict[str, Any]) -> Posting:
    org = h.get("organization") or {}
    offices = [o for o in as_list(h.get("offices")) if isinstance(o, dict)]
    countries = list(dict.fromkeys(str(o["country_code"]).upper() for o in offices
                                   if isinstance(o.get("country_code"), str) and len(o["country_code"]) == 2))
    cities = list(dict.fromkeys(o["city"] for o in offices if o.get("city")))
    location = "; ".join(", ".join(x for x in (o.get("city"), o.get("country")) if x) for o in offices)
    remote = h.get("remote")
    workplace = REMOTE_MAP.get(str(remote)) if remote else None
    missions = [m for m in as_list(h.get("key_missions")) if isinstance(m, str) and m.strip()]
    desc_parts = [html_to_text(h.get("summary"))]
    if missions:
        desc_parts.append("Key missions:\n" + "\n".join(f"- {m.strip()}" for m in missions))
    if h.get("profile"):
        desc_parts.append(html_to_text(h.get("profile")))
    prof = h.get("new_profession") or {}
    smin, smax = to_float(h.get("salary_minimum")), to_float(h.get("salary_maximum"))
    has_salary = smin is not None or smax is not None
    jid = str(first(h.get("reference"), h.get("objectID"), h.get("wk_reference")))
    org_slug, slug = org.get("slug"), h.get("slug")
    url = f"{SITE}/en/companies/{org_slug}/jobs/{slug}" if org_slug and slug else f"{SITE}/en/jobs?query={jid}"
    return Posting(
        source=source,
        source_job_id=jid,
        title=(h.get("name") or "").strip(),
        company=(org.get("name") or "").strip(),
        url=url,
        location_raw=location,
        countries=countries,
        cities=cities,
        workplace=workplace,
        description="\n\n".join(x for x in desc_parts if x),
        posted_at=parse_date(h.get("published_at")),
        employment_types=as_list(h.get("contract_type")),
        salary_min=smin,
        salary_max=smax,
        salary_currency=h.get("salary_currency") if has_salary else None,
        salary_period=PERIOD_MAP.get(str(h.get("salary_period"))) if has_salary else None,
        tags=[t for t in (prof.get("pivot_name"), prof.get("sub_category_name"), f"remote:{remote}" if remote else None) if t],
        raw={"language": h.get("language"), "remote": remote, "experience_min": h.get("experience_level_minimum"),
             "wk_reference": h.get("wk_reference")},
    )


@adapter("wttj")
def wttj(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    app, key = p.get("algolia_app"), p.get("algolia_key")
    if not (app and key):
        env = read_env(ctx.http.get(f"{SITE}/api/env").text)
        app = app or env.get("PUBLIC_ALGOLIA_APPLICATION_ID")
        key = key or env.get("PUBLIC_ALGOLIA_API_KEY_CLIENT")
    if not (app and key):
        ctx.notes.append("Algolia app/key not found in /api/env")
        return
    index = p.get("index", DEFAULT_INDEX)
    url = f"https://{app.lower()}-dsn.algolia.net/1/indexes/{index}/query"
    headers = {"X-Algolia-Application-Id": app, "X-Algolia-API-Key": key, "Content-Type": "application/json",
               "Referer": f"{SITE}/", "Origin": SITE}
    filters = build_filters(p)
    hits_per_page = int(p.get("hits_per_page", 100))
    seen: set[str] = set()
    for query in as_list(p.get("queries") or default_terms(4)):
        for page in range(int(p.get("max_pages", 2))):
            body = {
                "query": query, "page": page, "hitsPerPage": hits_per_page, "filters": filters,
                "attributesToRetrieve": ATTRS, "attributesToHighlight": [], "attributesToSnippet": [],
                "analytics": False,
            }
            try:
                data = ctx.http.post_json(url, headers=headers, content=json.dumps(body))
            except (Blocked, BudgetExceeded) as exc:
                ctx.notes.append(f"stopped: {exc}")
                return
            hits = data.get("hits") or []
            for h in hits:
                post = hit_to_posting(ctx.spec.id, h)
                if not post.title or post.source_job_id in seen:
                    continue
                seen.add(post.source_job_id)
                yield post
            if page + 1 >= int(data.get("nbPages") or 0) or len(hits) < hits_per_page:
                break
