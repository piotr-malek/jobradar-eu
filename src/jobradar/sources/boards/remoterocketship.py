"""Remote Rocketship (remoterocketship.com) — remote-only jobs from the SSR ``__NEXT_DATA__`` of its listing pages.

List: GET https://www.remoterocketship.com/country/<country>/jobs/<role>/   (trailing slash required, else a 308)
      → ``props.pageProps.initialJobOpenings`` (the 20 newest remote jobs for that country + role) and
      ``initialTotalJobCount``. There is no usable paging: ``?page=N`` returns page 1 again (the site's own
      paging calls ``/api/…``, which robots.txt disallows), so coverage = newest 20 per (country, role) page.
      ``country`` accepts ``europe`` (all of Europe), single countries, and ``jobs/<role>`` without country.
      robots.txt only blocks account/onboarding/api paths.
Rows have roleTitle, a two-line summary (no full description), techStack, requiredLanguages (ISO codes),
location (+ ``locationCountries`` the job is open to), seniority flags and an ATS ``url`` (the real application link).
No detail fetches: the listing already carries everything the site shows publicly.

Language mapping: ``requiredLanguages`` other than en/pl are appended to the description as
"German required." so ``language_requirements`` sees a strong cue and the matcher's language rule applies.

Params: countries [europe, poland, germany, netherlands], roles [data-engineer, analytics-engineer, bi-developer,
        data-architect]
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

from jobradar import config
from jobradar.http.client import Blocked, BudgetExceeded
from jobradar.models import Posting
from jobradar.sources.base import FetchContext, adapter, as_list, parse_date

SITE = "https://www.remoterocketship.com"
_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_LANG = {
    "de": "German", "fr": "French", "nl": "Dutch", "es": "Spanish", "it": "Italian", "pt": "Portuguese",
    "sv": "Swedish", "da": "Danish", "no": "Norwegian", "nb": "Norwegian", "fi": "Finnish", "cs": "Czech",
    "hu": "Hungarian", "ro": "Romanian",
}
_SENIORITY = (("isLead", "lead"), ("isSenior", "senior"), ("isMidLevel", "mid"), ("isJunior", "junior"),
              ("isEntryLevel", "entry"))


def parse_page(html: str) -> tuple[list[dict[str, Any]], int]:
    m = _NEXT.search(html)
    if not m:
        return [], 0
    try:
        pp = json.loads(m.group(1))["props"]["pageProps"]
    except (ValueError, KeyError, TypeError):
        return [], 0
    if not (pp.get("initialFilters") or {}).get("jobTitleFilters"):
        return [], 0  # unknown role slug for that country: the site silently serves ALL jobs (~11k) instead
    return pp.get("initialJobOpenings") or [], int(pp.get("initialTotalJobCount") or 0)


def to_posting(source: str, j: dict[str, Any]) -> Posting | None:
    jid, title = j.get("id"), (j.get("roleTitle") or "").strip()
    url = j.get("url") or ""
    if not jid or not title or not url:
        return None
    company = j.get("company") or {}
    location = (j.get("location") or "").strip()
    open_to = [c for c in (j.get("locationCountries") or []) if isinstance(c, str)]
    parts = [j.get("twoLineJobDescriptionSummary") or j.get("jobDescriptionSummary") or ""]
    stack = [s for s in (j.get("techStack") or []) if isinstance(s, str)]
    if stack:
        parts.append("Tech stack: " + ", ".join(stack) + ".")
    langs = [str(x).lower() for x in (j.get("requiredLanguages") or [])]
    for code in langs:
        if code in _LANG:
            parts.append(f"{_LANG[code]} required.")
    tags = [f"lang:{c}" for c in langs]
    tags += [t for flag, t in _SENIORITY if j.get(flag)]
    if j.get("employmentType"):
        tags.append(str(j["employmentType"]))
    wp = j.get("locationType")
    return Posting(
        source=source,
        source_job_id=str(jid),
        title=title,
        company=(company.get("name") or "").strip(),
        url=url,
        location_raw=location,
        workplace=wp if wp in ("remote", "hybrid", "onsite") else "remote",
        remote_scope=", ".join(open_to) or location or None,
        description=" ".join(p for p in parts if p).strip(),
        posted_at=parse_date(j.get("created_at")),
        employment_types=[str(j["employmentType"])] if j.get("employmentType") else [],
        tags=tags,
        raw={"slug": j.get("slug"), "company_slug": company.get("slug"), "langs": langs,
             "descriptionLanguage": j.get("descriptionLanguage")},
    )


@adapter("remoterocketship")
def remoterocketship(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    seen: set[str] = set()
    for country in as_list(p.get("countries", ["europe", "poland", "germany", "netherlands"])):
        for role in as_list(p.get("roles") or config.board_map("remoterocketship").get("roles", [])):
            try:
                resp = ctx.http.get(f"{SITE}/country/{country}/jobs/{role}/")
            except (Blocked, BudgetExceeded) as exc:
                ctx.notes.append(f"stopped: {exc}")
                return
            if resp.status_code != 200:
                ctx.notes.append(f"{country}/{role}: HTTP {resp.status_code}")
                continue
            rows, total = parse_page(resp.text)
            if total > len(rows):
                ctx.notes.append(f"{country}/{role}: {len(rows)} of {total} (no paging)")
            for j in rows:
                post = to_posting(ctx.spec.id, j)
                if post and post.source_job_id not in seen:
                    seen.add(post.source_job_id)
                    yield post
