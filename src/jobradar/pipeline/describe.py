"""Description finder: tonight's shortlisted jobs that came with a snippet or no text at all (Jooble, job-alert
e-mails, Jobgether...) get the full posting from somewhere else before the AI review reads them.

In order, cheapest first:
1. the same company and a similar title already in state.db with a full text (another board, a company ATS poll);
2. the job's route when it is a readable company ATS (Greenhouse, Lever, Ashby, Workable...): its job API;
3. the posting page's JobPosting JSON-LD, where robots.txt allows it; never on hosts that forbid or block robots.
Whatever is still short is reviewed on title, company and location (the screen caps its fit at 4)."""

from __future__ import annotations

import logging
import os
import time
from typing import Any
from urllib.parse import urlparse

from jobradar.normalize.names import norm_company, title_key

log = logging.getLogger(__name__)

SHORT = 500                  # characters: below this the review judges the title alone (apply/screen.SHORT_TEXT)
DESCRIBE_CAP = 80            # pages a night
# robots.txt disallows the job pages (jooble /desc/ and /away/, linkedin /) or the site challenges every bot
NEVER_OPEN = ("jooble.org", "linkedin.com", "pracuj.pl", "theprotocol.it", "indeed.", "glassdoor.", "stepstone.",
              "xing.com", "jobs.bg", "ejobs.ro", "jobs.cz", "profesia.")


def coverage(texts: list[str]) -> dict[str, int]:
    lengths = [len((t or "").strip()) for t in texts]
    return {"full": sum(n >= 1500 for n in lengths), "snippet": sum(0 < n < 1500 for n in lengths),
            "none": sum(n == 0 for n in lengths)}


def _similar(a: str, b: str) -> bool:
    from jobradar.pipeline.shortlist import _similar_titles

    return _similar_titles(title_key(a), title_key(b))


def from_store(store: Any, company: str, title: str) -> str:
    norm = norm_company(company)
    if not norm:
        return ""
    rows = store.query("SELECT title, description FROM v_postings_current WHERE company_norm = ? "
                       "AND LENGTH(description) >= ? ORDER BY LENGTH(description) DESC LIMIT 20", (norm, SHORT))
    return next((r["description"] for r in rows if _similar(title, r["title"] or "")), "")


def from_route(route: Any, http: Any) -> str:
    from jobradar.apply.forms import fetch_form

    if route is None or not route.readable:
        return ""
    spec = fetch_form(route.vendor, route.slug, route.job, http.for_vendor(route.vendor))
    return (spec.content if spec else "") or ""


def from_page(url: str, http: Any) -> str:
    from jobradar.apply.posting import read_posting

    host = urlparse(url or "").netloc.lower()
    if not host or any(h in host for h in NEVER_OPEN):
        return ""
    return read_posting(url, http).get("description") or ""


def describe(store: Any, items: list[Any], routes: dict[str, Any] | None = None, *, http: Any = None,
             deadline: float | None = None, cap: int | None = None) -> dict[str, Any]:
    """Fill `description` on short items in place; returns coverage before/after and where texts came from."""
    routes = routes or {}
    before = coverage([it.description for it in items])
    short = [it for it in items if len((it.description or "").strip()) < SHORT]
    cap = int(os.environ.get("JR_DESCRIBE_CAP") or (cap if cap is not None else DESCRIBE_CAP))
    found = {"store": 0, "route": 0, "page": 0}
    own = http is None and bool(short) and cap > 0
    if own:
        from jobradar.apply.prep import PrepHttp

        http = PrepHttp(max_requests=cap * 2)
    opened = 0
    try:
        for it in short:
            text = from_store(store, it.company, it.title)
            where = "store"
            if len(text) < SHORT and opened < cap and (deadline is None or time.monotonic() < deadline):
                route = routes.get(it.job_id)
                for where, fn, arg in (("route", from_route, route), ("page", from_page, it.url)):
                    if where == "route" and not (route and route.readable):
                        continue
                    opened += 1
                    try:
                        text = fn(arg, http)
                    except Exception as exc:  # noqa: BLE001 - one page never stops the night
                        log.info("describe %s failed for %s: %s", where, it.job_id, type(exc).__name__)
                        text = ""
                    if len(text) >= SHORT or opened >= cap:
                        break
            if len(text.strip()) >= SHORT and len(text) > len(it.description or ""):
                it.description = text
                found[where] += 1
    finally:
        if own and http is not None:
            http.close()
    return {"before": before, "after": coverage([it.description for it in items]), "found": found,
            "short": len(short), "opened": opened}
