"""DEV.BG: enrichment for the generic ``sitemap_jsonld`` adapter.

The JSON-LD swaps ``hiringOrganization.name`` (a dev.bg company URL) and ``sameAs`` (the name), leaves the
address empty and cuts the description to ~200 characters; the full text is in ``div.job_description``. The page's analytics attributes carry ``companyName`` and ``workFrom``: a city in Cyrillic, or
"Hybrid" / "Fully Remote".
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext
from jobradar.sources.generic.sitemap_jsonld import enricher

_ATTR = re.compile(r'"(companyName|workFrom)":("(?:[^"\\]|\\.)*")')
_MODES = {"hybrid": "hybrid", "fully remote": "remote", "remote": "remote"}
_CITIES = {"софия": "Sofia", "пловдив": "Plovdiv", "варна": "Varna", "бургас": "Burgas", "русе": "Ruse",
           "стара загора": "Stara Zagora", "велико търново": "Veliko Tarnovo", "благоевград": "Blagoevgrad",
           "плевен": "Pleven", "габрово": "Gabrovo", "шумен": "Shumen"}


@enricher("devbg_attrs")
def enrich_devbg(post: Posting, page: str, ctx: FetchContext) -> list[str]:
    attrs: dict[str, str] = {}
    for key, value in _ATTR.findall(page):
        attrs.setdefault(key, json.loads(value))
    if attrs.get("companyName"):
        post.company = attrs["companyName"]
    elif post.company.startswith("http"):
        post.company = ""
    where = (attrs.get("workFrom") or "").strip()
    mode = _MODES.get(where.lower())
    if mode:
        post.workplace = mode
        post.location_raw = post.location_raw or "Bulgaria"
    elif where:
        post.location_raw = f"{_CITIES.get(where.lower(), where)}, Bulgaria"
    post.countries = post.countries or ["BG"]
    body = BeautifulSoup(page, "lxml").select_one("div.job_description")
    text = html_to_text(str(body)) if body else ""
    if len(text) > len(post.description or ""):
        post.description = text
    return []
