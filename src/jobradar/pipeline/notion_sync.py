"""Notion sync, run by `jr daily`: the user's Status changes come in before the shortlist is built (so skipped jobs
never come back and are never reviewed), then shortlisted jobs go out as pages inside the night's time budget.

Order of the writes, best first: reviewed new jobs (highest fit first), the review of jobs already on a page
(written on an earlier night before the LLM got to them; a low fit trashes the page while it is still New), then
unreviewed new jobs. What doesn't fit tonight goes out the next night: nothing is dropped."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from jobradar.notion.client import NOT_RELEVANT, STATUS_TO_DECISION, NotionClient, NotionError, review_values

log = logging.getLogger(__name__)

NOTION_MINUTES = 6         # per night; ~3 pages a second, so about 1,000 pages
PULL_DAYS = 7              # Status changes are read back this far (decisions are de-duplicated)
SYNC_DAYS = 30             # older shortlisted jobs have lost their description (store maintenance)


def configured() -> bool:
    return bool(os.environ.get("NOTION_TOKEN") and os.environ.get("NOTION_JOBS_DB"))


def notion_minutes() -> float:
    from jobradar import config

    return float(os.environ.get("JR_NOTION_MINUTES")
                 or (config.user_settings().get("budgets") or {}).get("notion_minutes") or NOTION_MINUTES)


def pull_decisions(store: Any, client: NotionClient, *, now: datetime | None = None) -> int:
    """Status changes made in Notion → the `decisions` table (Not relevant = skipped with note `not_relevant`)."""
    now = now or datetime.now(timezone.utc)
    rows = []
    for p in client.changed_since(now - timedelta(days=PULL_DAYS)):
        status = STATUS_TO_DECISION.get(p.get("Status") or "")
        if not status or not p.get("Job ID"):
            continue
        rows.append({"job_id": p["Job ID"], "status": status, "at": p.get("_edited") or now,
                     "company": p.get("Company"), "note": "not_relevant" if p.get("Status") == NOT_RELEVANT else None})
    return store.import_decisions(rows)


def _rows(store: Any, where: str, order: str, since: Any) -> list[dict[str, Any]]:
    rows = store.query(
        f"""SELECT s.* FROM shortlist s LEFT JOIN v_decisions_current d USING (job_id)
            WHERE s.night >= :since AND {where} ORDER BY {order}""", {"since": since}, table="shortlist")
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if isinstance(r.get("copies"), str):
            try:
                r["copies"] = json.loads(r["copies"])
            except ValueError:
                r["copies"] = []
        out.setdefault(r["job_id"], r)
    return list(out.values())


def _mark(store: Any, row: dict[str, Any], page_id: str | None = None) -> None:
    store.execute("UPDATE shortlist SET notion_synced_at = :at" + (", notion_page_id = :page" if page_id else "")
                  + " WHERE job_id = :job_id AND night_id = :night_id",
                  {"at": datetime.now(timezone.utc), "page": page_id, "job_id": row["job_id"],
                   "night_id": row["night_id"]})


def push(store: Any, client: NotionClient, *, deadline: float | None = None,
         now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=SYNC_DAYS)).date()
    open_ = "s.hidden IS NULL AND COALESCE(d.status, 'new') != 'skipped'"
    phases = [
        ("create", _rows(store, f"s.notion_page_id IS NULL AND s.screened_at IS NOT NULL AND {open_}",
                         "s.fit DESC, s.night DESC, s.rank", since)),
        ("update", _rows(store, "s.notion_page_id NOT IN ('', 'unknown') AND s.notion_synced_at IS NULL AND s.screened_at IS NOT NULL",
                         "s.night DESC, s.rank", since)),
        ("create", _rows(store, f"s.notion_page_id IS NULL AND s.screened_at IS NULL AND {open_}",
                         "s.night DESC, s.rank", since)),
    ]
    stats = {"created": 0, "updated": 0, "trashed": 0, "failed": 0, "left": 0}
    for kind, rows in phases:
        for i, row in enumerate(rows):
            if deadline is not None and time.monotonic() >= deadline:
                stats["left"] += len(rows) - i
                break
            try:
                if kind == "create":
                    page = client.create_job(row)
                    _mark(store, row, page.get("id"))
                    stats["created"] += 1
                elif row.get("hidden") == "low_fit" and _still_new(store, row["job_id"]):
                    client.trash(row["notion_page_id"])
                    _mark(store, row)
                    stats["trashed"] += 1
                else:
                    client.set_values(row["notion_page_id"], review_values(row))
                    _mark(store, row)
                    stats["updated"] += 1
            except NotionError as exc:
                if exc.status in (0, 401, 403):
                    raise
                stats["failed"] += 1
                log.warning("notion %s failed for %s: %s", kind, row["job_id"], exc)
            except Exception as exc:  # noqa: BLE001 - a timeout on create may have landed; never retried
                stats["failed"] += 1
                if kind == "create":
                    _mark(store, row, "unknown")
                log.warning("notion %s failed for %s: %s", kind, row["job_id"], type(exc).__name__)
    return stats


def _still_new(store: Any, job_id: str) -> bool:
    rows = store.query("SELECT status FROM v_decisions_current WHERE job_id = ?", (job_id,))
    return not rows or rows[0]["status"] == "new"


def run_pull(store: Any) -> dict[str, Any]:
    """Statuses from Notion, then the filter suggestions the user ticked (they apply from tonight)."""
    if not configured():
        return {"status": "off"}
    from jobradar.pipeline import suggest

    with NotionClient() as client:
        out: dict[str, Any] = {"decisions_imported": pull_decisions(store, client)}
        try:
            out["filters"] = suggest.apply_ticked(store, client)
        except Exception as exc:  # noqa: BLE001 - suggestions are an extra; the night goes on
            log.warning("filter suggestions not applied: %s", exc)
        return out


def run_suggest(store: Any, llm: Any = None) -> dict[str, Any]:
    """New Not relevant jobs → proposed rules on the Filter suggestions page (one AI call at most)."""
    if not configured():
        return {"status": "off"}
    from jobradar.pipeline import suggest

    with NotionClient() as client:
        return suggest.propose(store, client, llm)


def run_push(store: Any, *, deadline: float | None = None) -> dict[str, Any]:
    if not configured():
        return {"status": "off"}
    with NotionClient() as client:
        added = client.ensure_jobs_schema()
        out = push(store, client, deadline=deadline)
        return {**out, "properties_added": added, "requests": client.requests}


def post_weekly(week: str, text: str) -> dict[str, Any]:
    """A "Job Radar <week>" page under the page that holds the Jobs database (NOTION_PARENT_PAGE)."""
    parent = os.environ.get("NOTION_PARENT_PAGE")
    if not configured() or not parent:
        return {"status": "off"}
    from jobradar.notion.client import paragraph, rich

    with NotionClient() as client:
        page = client.request("POST", "/pages", {
            "parent": {"type": "page_id", "page_id": parent},
            "properties": {"title": {"title": rich(f"Job Radar {week}: your week")}},
            "children": [paragraph(line) for line in text.splitlines() if line.strip()][:100]}, retry_unsafe=False)
    return {"status": "posted", "url": page.get("url")}
