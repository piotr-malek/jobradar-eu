"""Where the real application form is: ATS vendor + slug + job id from cluster members, else one polite look at
the aggregator page for an ATS link. Runs overnight (the shortlist row gets the route, apply URL and effort) and again in prep."""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import Any

from bs4 import BeautifulSoup

from jobradar.dedup.cluster import parse_ats
from jobradar.http.client import Blocked, BudgetExceeded, PoliteClient

log = logging.getLogger(__name__)

READABLE = ("greenhouse", "lever", "ashby", "workable", "recruitee", "erecruiter", "traffit")
EFFORT_MIN = {"greenhouse": 2, "lever": 2, "ashby": 2, "workable": 3, "recruitee": 3, "erecruiter": 3, "traffit": 3, "personio": 4, "teamtailor": 4,
              "bamboohr": 4, "breezy": 4, "smartrecruiters": 5, "linkedin": 3, "email": 5, "workday": 12, "unknown": 6}
_LINKEDIN = re.compile(r"linkedin\.com/jobs/view/(?:[^/?#]*-)?(?P<job>\d+)", re.I)
_ERECRUITER = re.compile(r"form\.erecruiter\.pl/form/(?P<job>[0-9a-f]{32})", re.I)   # Polish ATS, one form per job
_TRAFFIT = re.compile(r"(?P<slug>[\w-]+)\.traffit\.com/public/form/a/(?P<job>[0-9a-f]+)", re.I)   # PL, form token per advert


@dataclass
class Route:
    vendor: str
    url: str
    slug: str = ""
    job: str = ""
    via: str = ""

    @property
    def readable(self) -> bool:
        if self.vendor not in READABLE or not self.job:
            return False
        return self.vendor in ("workable", "erecruiter") or bool(self.slug)

    @property
    def effort_min(self) -> int:
        return EFFORT_MIN.get(self.vendor, EFFORT_MIN["unknown"])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Route":
        return cls(**{k: d.get(k) or "" for k in ("vendor", "url", "slug", "job", "via")})


def route_for_url(url: str | None, via: str = "") -> Route | None:
    if not url:
        return None
    if url.lower().startswith("mailto:"):
        return Route("email", url, via=via)
    parsed = parse_ats(url)
    if parsed:
        vendor, slug, job = parsed
        return Route(vendor, url, slug or "", job or "", via)
    m = _LINKEDIN.search(url)
    if m:
        return Route("linkedin", url, job=m.group("job"), via=via)
    m = _ERECRUITER.search(url)
    if m:
        return Route("erecruiter", url, job=m.group("job").lower(), via=via)
    m = _TRAFFIT.search(url)
    if m:
        return Route("traffit", url, m.group("slug").lower(), m.group("job").lower(), via)
    return None


def _rank(r: Route) -> tuple[int, int]:
    return (0 if r.readable else 1 if r.job else 2, 1 if r.vendor in ("linkedin", "email") else 0)


def route_for_members(members: list[dict[str, Any]]) -> Route:
    """Best route for a cluster: v1 ATS postings first, then any parseable member URL, else `unknown`."""
    for m in members:
        if m.get("ats_type") and m.get("ats_job_id"):
            url = m.get("apply_url") or m.get("url") or ""
            direct = route_for_url(url, m.get("source") or "")
            if direct and direct.vendor == m["ats_type"] == "traffit":   # the form needs its token, not the advert id
                return direct
            return Route(m["ats_type"], url, m.get("ats_slug") or "", str(m["ats_job_id"]), m.get("source") or "")
    found = [r for m in members for u in (m.get("apply_url"), m.get("url"))
             if (r := route_for_url(u, m.get("source") or ""))]
    if found:
        return sorted(found, key=_rank)[0]
    first = members[0] if members else {}
    return Route("unknown", first.get("apply_url") or first.get("url") or "", via=first.get("source") or "")


def resolve(route: Route, http: PoliteClient) -> Route:
    """For `unknown` routes: fetch the aggregator page once (robots respected) and look for an ATS link.
    Never raises; returns the input route when nothing better is found."""
    if route.vendor != "unknown" or not route.url.startswith("http"):
        return route
    try:
        resp = http.get(route.url)
    except (Blocked, BudgetExceeded) as exc:
        log.info("resolve skipped: %s", type(exc).__name__)
        return route
    except Exception as exc:  # noqa: BLE001 - never fail the run
        log.info("resolve failed: %s", type(exc).__name__)
        return route
    return route_in_page(resp, route.via) or route


def route_in_page(resp: Any, via: str = "") -> Route | None:
    """ATS route from a fetched page: its final URL after redirects, else the best ATS link on it."""
    final = route_for_url(str(resp.url), via)
    if final and final.vendor not in ("linkedin", "email"):
        return final
    if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", "html"):
        return None
    found = []
    for a in BeautifulSoup(resp.text, "lxml").find_all("a", href=True):
        r = route_for_url(a["href"], via)
        if r and r.vendor not in ("linkedin", "email"):
            found.append(r)
    return sorted(found, key=_rank)[0] if found else None
