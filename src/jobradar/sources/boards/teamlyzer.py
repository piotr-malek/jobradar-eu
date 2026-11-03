"""Teamlyzer (Portugal, IT-only) — list pages + schema.org JobPosting detail pages.

List:   GET https://pt.teamlyzer.com/companies/jobs?order=most_recent&page=N → 40 jobs per page in an ``ItemList``
        JSON-LD block (~34 pages, 1.3k live jobs). ``order=most_recent`` sorts by posting id, newest first, so a
        daily run only reads the first pages: paging stops after the first page that contains a stored job.
        The list has urls only; the title is the slug (``.../job/<title-slug>-<numeric id>``) and is gated with
        ``ctx.wants_detail`` before any detail request.
Detail: GET /companies/<co>/job/<slug>-<id> → JobPosting JSON-LD (English body behind a Portuguese wrapper line
        ``<title> na <company> (<role>, <cities…>, <work model>). …``; the wrapper is parsed for the work model and
        stripped, as is the trailing "reviews no Teamlyzer" boilerplate).
Apply:  GET /companies/get-job/<uuid>?v=company → 302 to the company's own ATS (utm_* appended). Only the redirect
        header is read (never followed), so it costs one request and no request to the ATS; utm params are stripped.
robots.txt: only ``/users``, ``/companies/h2h``, ``/companies/explore-companies``, ``/companies/*/comments``,
        ``/companies/industries/*`` and ``*?utm_*`` are disallowed for ``*``.

Params: max_pages 6, max_details 40, resolve_apply true, order most_recent, base_url.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime, timezone
from typing import Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobradar.http.client import Blocked, BudgetExceeded
from jobradar.models import Posting
from jobradar.sources.base import FetchContext, adapter, as_list, parse_date
from jobradar.sources.generic.sitemap_jsonld import _LD, extract_jobposting, jobposting_to_posting

BASE = "https://pt.teamlyzer.com"
_JOB_URL = re.compile(r"^https?://[^/]+/companies/[^/]+/job/(?P<slug>[a-z0-9-]*?)-?(?P<id>\d+)/?$", re.I)
_MODES = {"hibrido": "hybrid", "full remote": "remote", "remote first": "remote", "escritorio": "onsite"}
_REVIEWS_TAIL = re.compile(r"\s*(?:Tecnologias:[^.]*\.\s*)?A\s.{1,120}?\stem \d+ reviews? no Teamlyzer.*$", re.S)


def _fold(s: str) -> str:
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn").strip()


def parse_list(html: str) -> list[str]:
    """Job URLs of one list page, in page order (ItemList JSON-LD)."""
    urls: list[str] = []
    for m in _LD.finditer(html):
        try:
            data = json.loads(m.group(1).strip(), strict=False)
        except ValueError:
            continue
        for node in as_list(data):
            if isinstance(node, dict) and node.get("@type") == "ItemList":
                for el in node.get("itemListElement") or []:
                    u = el.get("url") if isinstance(el, dict) else None
                    if u and _JOB_URL.match(u) and u not in urls:
                        urls.append(u)
    return urls


def _clean_description(desc: str, title: str, company: str) -> tuple[str, str | None]:
    """Strip the Portuguese wrapper line and review boilerplate → (text, workplace)."""
    workplace = None
    head = f"{title} na {company} ("
    if desc.startswith(head):
        end = desc.find("). ", len(head))
        if end == -1 and desc.endswith(")."):
            end = len(desc) - 2
        if end != -1:
            parts = [x.strip() for x in desc[len(head):end].split(",")]
            workplace = _MODES.get(_fold(parts[-1])) if parts else None
            desc = desc[end + 3:]
    return _REVIEWS_TAIL.sub("", desc).strip(), workplace


def parse_detail(html: str, url: str, source: str, job_id: str) -> Posting | None:
    jp = extract_jobposting(html)
    if not jp:
        return None
    post = jobposting_to_posting(jp, source=source, source_job_id=job_id, url=url)
    post.description, mode = _clean_description(post.description, post.title, post.company)
    if mode:
        post.workplace = mode
    ident = jp.get("identifier")
    uuid = ident.get("value") if isinstance(ident, dict) else None
    post.raw = {**(post.raw or {}), "uuid": uuid, "role": jp.get("occupationalCategory")}
    valid = parse_date(jp.get("validThrough"))
    if valid and valid < datetime.now(timezone.utc):
        return None
    return post


def strip_utm(url: str) -> str:
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if not k.lower().startswith("utm_") and k.lower() not in ("ref", "source")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), ""))


def _resolve_apply(ctx: FetchContext, base: str, uuid: str) -> str | None:
    resp = ctx.http.get(f"{base}/companies/get-job/{uuid}?v=company", follow_redirects=False)
    loc = resp.headers.get("location") if resp.status_code in (301, 302, 303, 307, 308) else None
    if not loc or urlsplit(loc).netloc.endswith("teamlyzer.com"):
        return None
    return strip_utm(loc)


@adapter("teamlyzer")
def teamlyzer(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    base = str(p.get("base_url", BASE)).rstrip("/")
    max_pages = int(p.get("max_pages", 6))
    max_details = int(p.get("max_details", 40))
    resolve = bool(p.get("resolve_apply", True))
    order = p.get("order", "most_recent")
    pending: list[tuple[str, str]] = []  # (url, id)
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        try:
            resp = ctx.http.get(f"{base}/companies/jobs", params={"order": order, "page": page})
        except (Blocked, BudgetExceeded) as exc:
            ctx.notes.append(f"list stopped: {exc}")
            break
        if resp.status_code != 200:
            ctx.notes.append(f"list HTTP {resp.status_code}")
            break
        urls = parse_list(resp.text)
        if not urls:
            break
        known_hit = False
        for url in urls:
            m = _JOB_URL.match(url)
            if not m or m.group("id") in seen:
                continue
            jid = m.group("id")
            seen.add(jid)
            if ctx.is_known(ctx.spec.id, jid):
                ctx.mark_seen(ctx.spec.id, jid)
                known_hit = True
            elif not ctx.wants_detail(m.group("slug").replace("-", " ")):
                ctx.skipped += 1
            else:
                pending.append((url, jid))
        if known_hit and order == "most_recent":
            break  # everything older than a stored job was handled by an earlier run
    if len(pending) > max_details:
        ctx.notes.append(f"{len(pending) - max_details} new relevant jobs beyond max_details")
    for url, jid in pending[:max_details]:
        try:
            resp = ctx.http.get(url)
            if resp.status_code != 200:
                continue
            post = parse_detail(resp.text, url, ctx.spec.id, jid)
            if post is None:
                continue
            uuid = (post.raw or {}).get("uuid")
            if resolve and uuid:
                try:
                    post.apply_url = _resolve_apply(ctx, base, uuid)
                except Blocked as exc:
                    ctx.notes.append(f"apply resolve stopped: {exc}")
                    resolve = False
        except (Blocked, BudgetExceeded) as exc:
            ctx.notes.append(f"detail stopped: {exc}")
            break
        yield post
