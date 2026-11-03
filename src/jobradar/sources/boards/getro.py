"""Getro-hosted VC portfolio job boards (Atomico, Point Nine, Creandum, Speedinvest, Earlybird, MMC, OpenOcean,
firstminute, byFounders, Techstars, Accel …) — company jobs from the boards' own public pages.

Getro's JSON API (api.getro.com) is disallowed by its robots.txt, so it is NOT used. Allowed surfaces only:

1. ``https://<host>/sitemap.xml`` (index or urlset; the ``sitemap_jobs*.xml`` children of an index are expanded)
   lists every job as ``/companies/<org>/jobs/<id>-<title-slug>`` with ``lastmod``. The location is not in it.
2. The slug is the title gate (``ctx.wants_detail``), then the detail page's SSR ``__NEXT_DATA__``
   (``props.pageProps.initialState.jobs.currentJob``) gives title, HTML description, locations, organization,
   postedAt, compensation, employment types and ``url`` = the company's own ATS posting (the apply link).

The job id is global across Getro boards (portfolio overlap: the same job sits on several boards), so it is used as
``source_job_id`` and a job is fetched and stored once per run and ever after, whichever board lists it first.

Budget: ``max_details`` detail pages per run, spread round-robin over the boards (a board dict may carry its own
smaller ``max_details``); the first-run backlog therefore drains over several runs, newest ``lastmod`` first.
Known postings are skipped without a request; closed/inactive jobs are dropped.

Params: boards [host | {host, max_details}], max_details 120, lastmod_days 90 (skip stale sitemap rows),
        sitemap_include (regex on child sitemap URLs of an index, default ``jobs``)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.parse import urlsplit

from jobradar.dedup.cluster import parse_ats
from jobradar.http.client import Blocked, BudgetExceeded
from jobradar.models import Posting
from jobradar.normalize.location import extract_places
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, as_list, parse_date
from jobradar.sources.generic.sitemap_jsonld import decode_body, parse_sitemap

_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_JOB_URL = re.compile(r"^https?://[^/]+/companies/(?P<org>[^/]+)/jobs/(?P<id>\d+)-(?P<slug>[^/?#]+)$")
_PERIOD = {"year": "year", "month": "month", "day": "day", "hour": "hour", "week": None}
_REMOTE = re.compile(r"^\s*(?:remote|anywhere|worldwide)\b", re.I)
_MODES = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on_site": "onsite", "on-site": "onsite",
          "in_office": "onsite"}


@dataclass
class Candidate:
    host: str
    url: str
    job_id: str
    org: str
    slug: str
    lastmod: datetime | None


def parse_job_sitemap(host: str, entries: list[Any]) -> list[Candidate]:
    out = []
    for e in entries:
        m = _JOB_URL.match(e.loc)
        if m:
            out.append(Candidate(host, e.loc, m["id"], m["org"], m["slug"], e.lastmod))
    return out


def current_job(html: str) -> dict[str, Any] | None:
    m = _NEXT.search(html)
    if not m:
        return None
    try:
        job = json.loads(m.group(1))["props"]["pageProps"]["initialState"]["jobs"]["currentJob"]
    except (ValueError, KeyError, TypeError):
        return None
    return job if isinstance(job, dict) and job.get("id") and job.get("title") else None


def _locations(job: dict[str, Any]) -> list[str]:
    out = []
    for loc in as_list(job.get("locations")):
        name = loc.get("name") or loc.get("description") if isinstance(loc, dict) else loc
        if name and str(name).strip() not in out:
            out.append(str(name).strip())
    return out


def to_posting(source: str, cand: Candidate, job: dict[str, Any]) -> Posting | None:
    if (job.get("status") or "active") != "active" or job.get("closedAt") or job.get("deactivatedAt"):
        return None
    title = (job.get("title") or "").strip()
    org = job.get("organization") or {}
    locs = _locations(job)
    places = [x for x in locs if not _REMOTE.match(x)]
    remote_listed = len(places) != len(locs)
    mode = _MODES.get(str(job.get("workMode") or job.get("work_mode") or "").lower())
    workplace = mode or ("remote" if remote_listed else None)
    countries, _regions, cities = extract_places(", ".join(places))

    apply = job.get("applicationPath") if job.get("applicationMethod") == "url" else None
    apply = apply or job.get("url") or None
    if apply and (urlsplit(apply).netloc.lower() == cand.host or urlsplit(apply).netloc.lower().endswith("getro.com")):
        apply = None  # the board's own page is not an application link
    ats = parse_ats(apply)

    cents = lambda v: round(v / 100, 2) if isinstance(v, (int, float)) and v > 0 else None  # noqa: E731
    smin, smax = cents(job.get("compensationAmountMinCents")), cents(job.get("compensationAmountMaxCents"))
    period = _PERIOD.get(str(job.get("compensationPeriod") or ""))
    functions = [re.sub(r"[()]", "", f.get("name", "")).strip() for f in as_list(job.get("jobFunctions"))
                 if isinstance(f, dict)]
    return Posting(
        source=source,
        source_job_id=str(job["id"]),
        title=title,
        company=(org.get("name") or cand.org).strip(),
        url=cand.url,
        apply_url=apply,
        location_raw=" | ".join(locs),
        countries=sorted(countries),
        cities=list(cities),
        workplace=workplace,
        remote_scope=(", ".join(places) or None) if remote_listed else None,
        description=html_to_text(job.get("description") or ""),
        posted_at=parse_date(job.get("postedAt")),
        employment_types=[str(x) for x in as_list(job.get("employmentTypes"))],
        salary_min=smin,
        salary_max=smax,
        salary_currency=job.get("compensationCurrency") if (smin or smax) else None,
        salary_period=period if (smin or smax) else None,
        tags=[f for f in functions if f],
        ats_type=ats[0] if ats else None,
        ats_slug=(ats[1] or None) if ats else None,
        ats_job_id=ats[2] if ats else None,
        raw={"board": cand.host, "org": cand.org, "getro_id": job.get("id"), "source": job.get("source")},
    )


def _board_specs(p: dict[str, Any]) -> list[tuple[str, int | None]]:
    out = []
    for b in as_list(p.get("boards")):
        if isinstance(b, dict):
            out.append((b["host"], b.get("max_details")))
        else:
            out.append((str(b), None))
    return out


def _board_candidates(ctx: FetchContext, host: str, include: re.Pattern[str]) -> list[Candidate]:
    resp = ctx.http.get(f"https://{host}/sitemap.xml")
    if resp.status_code != 200:
        ctx.notes.append(f"{host}: sitemap HTTP {resp.status_code}")
        return []
    children, urls = parse_sitemap(decode_body(resp.content))
    for child in children:
        if include.search(child.loc):
            r = ctx.http.get(child.loc)
            if r.status_code == 200:
                urls += parse_sitemap(decode_body(r.content))[1]
            else:
                ctx.notes.append(f"{host}: {child.loc.rsplit('/', 1)[-1]} HTTP {r.status_code}")
    return parse_job_sitemap(host, urls)


@adapter("getro")
def getro(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    include = re.compile(p.get("sitemap_include", "jobs"), re.I)
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(p.get("lastmod_days", 90)))
    boards = _board_specs(p)
    queues: dict[str, list[Candidate]] = {}
    caps: dict[str, int | None] = dict(boards)
    seen_ids: set[str] = set()
    total_rows = 0
    try:
        for host, _cap in boards:
            try:
                cands = _board_candidates(ctx, host, include)
            except Blocked as exc:
                ctx.notes.append(f"{host}: {exc}")
                continue
            total_rows += len(cands)
            cands.sort(key=lambda c: c.lastmod or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            queue = []
            for c in cands:
                if c.job_id in seen_ids:
                    continue  # portfolio overlap: already listed by an earlier board
                if c.lastmod and c.lastmod < cutoff:
                    ctx.skipped += 1
                    continue
                if ctx.is_known(ctx.spec.id, c.job_id):
                    seen_ids.add(c.job_id)
                    ctx.mark_seen(ctx.spec.id, c.job_id)
                    continue
                if not ctx.wants_detail(c.slug.replace("-", " ")):
                    seen_ids.add(c.job_id)
                    ctx.skipped += 1
                    continue
                seen_ids.add(c.job_id)
                queue.append(c)
            queues[host] = queue
    except BudgetExceeded as exc:
        ctx.notes.append(f"stopped during sitemaps: {exc}")
        return

    budget = int(p.get("max_details", 120))
    used = {h: 0 for h in queues}
    pending = sum(len(q) for q in queues.values())
    done: set[str] = set()
    fetched = 0
    stop = False
    while fetched < budget and not stop and any(queues[h] for h in queues):
        for host in list(queues):
            q = queues[host]
            cap = caps.get(host)
            if not q or (cap is not None and used[host] >= cap) or fetched >= budget:
                continue
            cand = q.pop(0)
            if cand.job_id in done:
                continue
            try:
                resp = ctx.http.get(cand.url)
            except (Blocked, BudgetExceeded) as exc:
                ctx.notes.append(f"stopped: {exc}")
                stop = True
                break
            used[host] += 1
            fetched += 1
            if resp.status_code != 200:
                ctx.notes.append(f"{host}: detail HTTP {resp.status_code}")
                continue
            job = current_job(resp.text)
            post = to_posting(ctx.spec.id, cand, job) if job else None
            if post:
                done.add(cand.job_id)
                yield post
        # a capped board never yields again: drop it so the loop can end
        for host in queues:
            cap = caps.get(host)
            if cap is not None and used[host] >= cap:
                queues[host] = []
    left = sum(len(q) for q in queues.values())
    ctx.notes.append(f"{total_rows} sitemap jobs on {len(boards)} boards, {pending} new + title-relevant, "
                     f"{fetched} detail pages fetched, {left} left for the next runs")
