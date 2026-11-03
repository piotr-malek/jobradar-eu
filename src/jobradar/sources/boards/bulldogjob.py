"""Bulldogjob.pl: enrichment for the generic ``sitemap_jsonld`` adapter.

robots.txt disallows ``/feeds`` (RSS) but publishes ``/sitemap.xml.gz`` → ``/pl/jobs.xml.gz``; offer
pages carry a JSON-LD JobPosting. The JSON-LD has no work mode / contract form, so this hook reads the
page's own ``__NEXT_DATA__`` job object: ``workModes`` / ``remote``, ``contractB2b`` / ``contractEmployment``
and the per-contract salary brackets.
"""

from __future__ import annotations

import json
import re
from typing import Any

from jobradar.models import Posting
from jobradar.sources.base import FetchContext, first, to_float
from jobradar.sources.generic.sitemap_jsonld import enricher, salary_period

_NEXT = re.compile(r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", re.S)


def next_job(page: str) -> dict[str, Any] | None:
    m = _NEXT.search(page)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    job = (((data.get("props") or {}).get("pageProps") or {}).get("data") or {}).get("job")
    return job if isinstance(job, dict) else None


def _bracket(value: Any) -> dict[str, Any] | None:
    """Salary objects look like {currency, minValue, maxValue, timeframe: 'month', money: 'From 7 500', hidden}."""
    if not isinstance(value, dict) or value.get("hidden"):
        return None
    lo = to_float(first(value.get("minValue"), value.get("from")))
    hi = to_float(first(value.get("maxValue"), value.get("to")))
    if lo is None and hi is None:
        return None
    return {"min": lo, "max": hi, "currency": value.get("currency"),
            "period": first(value.get("timeframe"), value.get("timeUnit"), value.get("period"))}


@enricher("bulldogjob_next")
def enrich_bulldogjob(post: Posting, page: str, ctx: FetchContext) -> list[str]:
    job = next_job(page)
    if not job:
        return []
    modes = [str(m).lower() for m in job.get("workModes") or []]
    if job.get("remote") is True or "remote" in modes or "full-remote" in modes:
        post.workplace = "remote"
    elif "hybrid" in modes:
        post.workplace = "hybrid"
    elif modes and all(m in ("on-site", "onsite", "office") for m in modes):
        post.workplace = "onsite"

    contracts = []
    if job.get("contractB2b"):
        contracts.append("b2b")
    if job.get("contractEmployment"):
        contracts.append("uop")
    if job.get("contractOther"):
        contracts.append("other")
    sal = first(_bracket(job.get("b2bSalary")), _bracket(job.get("employmentSalary")), _bracket(job.get("otherSalary")))
    if sal and post.salary_min is None and post.salary_max is None:
        post.salary_min, post.salary_max = sal["min"], sal["max"]
        post.salary_currency = (sal.get("currency") or "").upper() or post.salary_currency or "PLN"
        post.salary_period = salary_period(sal.get("period")) or "month"
    post.tags = list(dict.fromkeys(list(job.get("technologyTags") or []) + contracts + post.tags))
    if job.get("employmentType") and job["employmentType"] not in post.employment_types:
        post.employment_types.append(job["employmentType"])
    post.raw = {
        **(post.raw or {}),
        "workModes": modes,
        "experienceLevel": job.get("experienceLevel"),
        "roles": job.get("roles"),
        "endsAt": job.get("endsAt"),
        "salary": sal,
    }
    return []
