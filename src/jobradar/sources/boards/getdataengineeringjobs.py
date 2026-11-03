"""getdataengineeringjobs.com: enrichment for the generic ``sitemap_jsonld`` adapter.

The JSON-LD has the employer's office as ``jobLocation``, no work mode for hybrid or on-site jobs, and no
remote eligibility: a job shown as "Remote · Worldwide" carries only Buenos Aires, which the matcher reads as
"remote outside EU". The page's facts list (``<dl class="facts">``) has both: "Work arrangement" (Remote /
Hybrid / On-site, comma-joined when several) and "Remote eligibility" (Worldwide, a region or a country list,
or "No restriction stated").
"""

from __future__ import annotations

import html as html_lib
import re

from jobradar.models import Posting
from jobradar.normalize.text import fold
from jobradar.sources.base import FetchContext
from jobradar.sources.generic.sitemap_jsonld import country_iso2, enricher

_FACTS = re.compile(r"<dl class=\"facts\">(.*?)</dl>", re.S)
_PAIR = re.compile(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", re.S)
_TAG = re.compile(r"<[^>]+>")
_UNSTATED = {"not specified", "no restriction stated"}


def _text(value: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(_TAG.sub("", value))).strip()


def facts(page: str) -> dict[str, str]:
    """The facts list as {label: value}, without "Not specified" rows."""
    m = _FACTS.search(page)
    if not m:
        return {}
    out: dict[str, str] = {}
    for label, value in _PAIR.findall(m.group(1)):
        value = _text(value)
        if value and fold(value) not in _UNSTATED:
            out[_text(label)] = value
    return out


@enricher("gdej_facts")
def enrich_gdej(post: Posting, page: str, ctx: FetchContext) -> list[str]:
    f = facts(page)
    modes = {re.sub(r"[^a-z]", "", fold(m)) for m in f.get("Work arrangement", "").split(",")}
    if "remote" in modes:
        post.workplace = "remote"
    elif "hybrid" in modes:
        post.workplace = "hybrid"
    elif "onsite" in modes:
        post.workplace = "onsite"
    scope = f.get("Remote eligibility")
    if post.workplace == "remote" and scope:
        # where the candidate may live replaces the office, which would decide the location verdict otherwise
        post.remote_scope = scope
        post.location_raw = f"Remote ({scope})"
        post.countries = list(dict.fromkeys(iso for part in scope.split(",") if (iso := country_iso2(part))))
    post.raw = {**(post.raw or {}), "facts": f}
    return []
