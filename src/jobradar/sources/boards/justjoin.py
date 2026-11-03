"""JustJoin.it / RocketJobs.pl (same platform): enrichment for the generic ``sitemap_jsonld`` adapter.

robots.txt disallows ``/api/`` (and api.justjoin.it entirely), so these boards are read via the public
active-jobs sitemap + offer pages. The offer page's JSON-LD lacks work mode and contract types and its
description has paragraph breaks squashed, so this hook reads the offer object the page itself embeds
in its React Server Components payload (``self.__next_f.push``) and fills:

- workplace (remote / hybrid / onsite from ``workplaceType``),
- contract types + salary per contract (original currency, b2b preferred, unit → period),
- description from the HTML ``body`` (proper paragraphs), apply URL, skills, all cities,
- the other-city copies of a multi-location offer (returned so the adapter skips them).
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, first, parse_date, to_float
from jobradar.sources.generic.sitemap_jsonld import contract_tags, enricher, salary_period

_PUSH = re.compile(r"self\.__next_f\.push\(\[1,\s*(\"(?:[^\"\\]|\\.)*\")\s*\]\)", re.S)
_WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "office": "onsite", "onsite": "onsite"}
_CONTRACT = {"b2b": "b2b", "permanent": "uop", "mandate_contract": "uz", "mandate": "uz",
             "specific-task_contract": "uod", "specific_task_contract": "uod"}


def rsc_payload(page: str) -> str:
    """Concatenated Next.js flight data (the strings pushed to ``self.__next_f``)."""
    parts = []
    for m in _PUSH.finditer(page):
        try:
            parts.append(json.loads(m.group(1)))
        except ValueError:
            continue
    return "".join(parts)


def rsc_text_chunk(payload: str, ref: str) -> str | None:
    """Resolve a ``"$81"`` reference to its ``81:T<hexlen>,<text>`` chunk (length counted in UTF-8 bytes)."""
    key = ref.lstrip("$")
    m = re.search(rf"(?<![0-9a-zA-Z]){re.escape(key)}:T([0-9a-f]+),", payload) or re.search(
        rf"{re.escape(key)}:T([0-9a-f]+),", payload)
    if not m:
        return None
    n = int(m.group(1), 16)
    return payload[m.end():].encode("utf-8")[:n].decode("utf-8", errors="ignore")


def extract_offer(page: str) -> tuple[dict[str, Any] | None, str]:
    payload = rsc_payload(page)
    i = payload.find('"offer":{')
    if i < 0:
        return None, payload
    try:
        offer, _ = json.JSONDecoder().raw_decode(payload, i + len('"offer":'))
    except ValueError:
        return None, payload
    return (offer if isinstance(offer, dict) else None), payload


def _salary(types: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[str]]:
    originals = [t for t in types if isinstance(t, dict) and t.get("currencySource") in (None, "original")]
    kinds = list(dict.fromkeys(str(t.get("type") or "") for t in originals if t.get("type")))
    with_amount = [t for t in originals if to_float(t.get("from")) or to_float(t.get("to"))]
    with_amount.sort(key=lambda t: 0 if t.get("type") == "b2b" else 1)
    return (with_amount[0] if with_amount else None), kinds


@enricher("justjoin_rsc")
def enrich_justjoin(post: Posting, page: str, ctx: FetchContext) -> list[str]:
    offer, payload = extract_offer(page)
    if not offer:
        ctx.log.debug("justjoin: no offer object in %s", post.url)
        return []
    wp = offer.get("workplaceType")
    wp_value = (wp.get("value") if isinstance(wp, dict) else wp) or ""
    post.workplace = _WORKPLACE.get(str(wp_value).lower(), post.workplace)
    if post.workplace == "remote" and not post.remote_scope:
        post.remote_scope = offer.get("countryCode") or None

    body = offer.get("body")
    if isinstance(body, str) and body.startswith("$"):
        body = rsc_text_chunk(payload, body)
    if isinstance(body, str) and len(body) > 40:
        post.description = html_to_text(body)

    sal, kinds = _salary(offer.get("employmentTypes") or [])
    if sal:
        post.salary_min = to_float(sal.get("from"))
        post.salary_max = to_float(sal.get("to"))
        post.salary_currency = (sal.get("currency") or "").upper() or None
        post.salary_period = salary_period(sal.get("unit")) or post.salary_period
    wt = offer.get("workingTime")
    working = (wt.get("value") if isinstance(wt, dict) else wt) or None
    post.employment_types = list(dict.fromkeys(kinds + ([working] if working else []))) or post.employment_types

    post.apply_url = offer.get("applyUrl") or post.apply_url
    if not post.company:
        post.company = offer.get("companyName") or ""
    cc = offer.get("countryCode")
    if cc and isinstance(cc, str) and len(cc) == 2 and cc.upper() not in post.countries:
        post.countries.append(cc.upper())
    siblings: list[str] = []
    parts = urlsplit(post.url)
    base = parts.path.rsplit("/", 1)[0]
    for loc in offer.get("multilocation") or []:
        if not isinstance(loc, dict):
            continue
        city = loc.get("city")
        if city and city not in post.cities:
            post.cities.append(city)
        if loc.get("slug"):
            siblings.append(f"{parts.scheme}://{parts.netloc}{base}/{loc['slug']}")
    if len(post.cities) > 1:
        post.location_raw = "; ".join(post.cities) + (f", {cc.upper()}" if cc else "")

    skills = [s.get("name") for s in (offer.get("requiredSkills") or []) + (offer.get("niceToHaveSkills") or [])
              if isinstance(s, dict) and s.get("name")]
    contracts = [_CONTRACT[k] for k in kinds if k in _CONTRACT]
    post.tags = list(dict.fromkeys(skills + contracts + contract_tags(post.description) + post.tags))
    exp = offer.get("experienceLevel")
    cat = offer.get("category")
    post.raw = {
        **(post.raw or {}),
        "workplaceType": wp_value or None,
        "experienceLevel": exp.get("value") if isinstance(exp, dict) else exp,
        "category": cat.get("key") if isinstance(cat, dict) else cat,
        "languages": offer.get("languages"),
        "offerParent": (offer.get("offerParent") or {}).get("slug") if isinstance(offer.get("offerParent"), dict) else None,
        "guid": offer.get("guid"),
        "expiredAt": offer.get("expiredAt"),
        "remoteInterview": offer.get("remoteInterview"),
    }
    if not post.posted_at:
        post.posted_at = parse_date(first(offer.get("publishedAt"), offer.get("lastPublishedAt")))
    return siblings
