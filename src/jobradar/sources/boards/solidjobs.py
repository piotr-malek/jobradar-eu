"""SOLID.Jobs: enrichment for the generic ``sitemap_jsonld`` adapter.

robots.txt allows everything except /management/ and /admin/; ``/sitemap-offers.xml`` lists offers
(lastmod = publication day, expired offers linger ~months → use ``lastmod_days``). The JSON-LD only
flags fully-remote offers (TELECOMMUTE) and gives a salary normalised to PLN/month, so this hook reads
the Angular ``ng-state`` ``jobOfferDetails`` the page embeds: ``remotePossible`` (W całości / Hybrydowo /
Częściowo / Brak) and the advertised salary range + contract form.
"""

from __future__ import annotations

import json
import re
from typing import Any

from jobradar.models import Posting
from jobradar.normalize.text import fold
from jobradar.sources.base import FetchContext, to_float
from jobradar.sources.generic.sitemap_jsonld import enricher, salary_period

_STATE = re.compile(r"<script[^>]+id=[\"']ng-state[\"'][^>]*>(.*?)</script>", re.S)
_REMOTE = {"w calosci": "remote", "calkowicie": "remote", "hybrydowo": "hybrid", "czesciowo": "hybrid",
           "brak": "onsite", "nie": "onsite"}


def _contract(value: Any) -> str | None:
    v = fold(str(value or ""))
    if "b2b" in v:
        return "b2b"
    if "o prace" in v:
        return "uop"
    if "zleceni" in v:
        return "uz"
    if "o dzielo" in v:
        return "uod"
    return None


def offer_details(page: str) -> dict[str, Any] | None:
    m = _STATE.search(page)
    if not m:
        return None
    try:
        state = json.loads(m.group(1))
    except ValueError:
        return None
    for value in state.values():
        body = value.get("b") if isinstance(value, dict) else None
        if isinstance(body, dict) and isinstance(body.get("jobOfferDetails"), dict):
            return body["jobOfferDetails"]
    return None


@enricher("solidjobs_ngstate")
def enrich_solidjobs(post: Posting, page: str, ctx: FetchContext) -> list[str]:
    d = offer_details(page)
    if not d:
        return []
    mode = _REMOTE.get(fold(str(d.get("remotePossible") or "")).strip())
    if mode and not (post.workplace == "remote" and mode == "onsite"):
        post.workplace = mode
    contracts: list[str] = []
    for i, rng in enumerate((d.get("salaryRange"), d.get("secondarySalaryRange"))):
        if not isinstance(rng, dict):
            continue
        c = _contract(rng.get("employmentType"))
        if c:
            contracts.append(c)
        lo, hi = to_float(rng.get("lowerBound")), to_float(rng.get("upperBound"))
        if i == 0 and (lo or hi):  # advertised range (JSON-LD carries the PLN/month normalisation)
            post.salary_min, post.salary_max = lo, hi
            post.salary_currency = rng.get("currency") or post.salary_currency
            post.salary_period = salary_period(rng.get("salaryPeriod")) or post.salary_period
    post.tags = list(dict.fromkeys(post.tags + contracts))
    post.raw = {**(post.raw or {}), "remotePossible": d.get("remotePossible"),
                "experienceLevel": d.get("experienceLevel"), "validTo": d.get("validTo"),
                "category": [d.get("mainCategory"), d.get("subCategory")]}
    return []
