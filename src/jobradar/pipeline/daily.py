"""`jr daily`: what the overnight workflow runs.

1. import the user's decisions from Notion (the Status of each page), so skipped jobs never come back
2. fetch the sources due tonight, within the run's time budget (the rest stay due and run next time)
3. re-match recent postings when the rules changed (new rule_version)
4. build the shortlist (route, screen when switched on) into state.db
5. upkeep: trim old descriptions and sightings
6. optional "N new jobs" e-mail
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jobradar import config
from jobradar.match.engine import MatchEngine
from jobradar.models import Posting, utcnow
from jobradar.pipeline import notion_sync
from jobradar.pipeline.run import match_row
from jobradar.sources.base import parse_date
from jobradar.sources.registry import load_specs

log = logging.getLogger(__name__)
DEFAULT_MINUTES = {"radar": 20, "apply": 25}
RUN_MINUTES = 30           # the workflow stops the job at 35 minutes; install and the state push take the rest
NOTION_RESERVE_MINUTES = 4 # the screen stops this early so the night's pages still go out


def posting_from_row(r: dict[str, Any]) -> Posting:
    return Posting(
        source=r["source"], source_job_id=r.get("source_job_id") or "", title=r.get("title") or "",
        company=r.get("company") or "", url=r.get("url") or "", apply_url=r.get("apply_url"),
        location_raw=r.get("location_raw") or "", countries=list(r.get("countries") or []),
        cities=list(r.get("cities") or []), workplace=r.get("workplace"), remote_scope=r.get("remote_scope"),
        description=r.get("description") or "", posted_at=parse_date(r.get("posted_at")),
        employment_types=list(r.get("employment_types") or []), salary_min=r.get("salary_min"),
        salary_max=r.get("salary_max"), salary_currency=r.get("salary_currency"),
        salary_period=r.get("salary_period"), tags=list(r.get("tags") or []), ats_type=r.get("ats_type"),
        ats_slug=r.get("ats_slug"), ats_job_id=r.get("ats_job_id"),
    )


def ensure_rule_version(store: Any, engine: MatchEngine | None = None, *, days: int = 30, force: bool = False) -> int:
    """When the matching rules changed (packs, profile, roles, overrides), re-match recent postings."""
    engine = engine or MatchEngine.load()
    if not force and store.rule_version_known(engine.rule_version):
        return 0
    specs = load_specs(all_sources=True)
    rows = store.latest_versions(utcnow() - timedelta(days=days))
    now = utcnow()
    matches = []
    for r in rows:
        p = posting_from_row(r)
        spec = specs.get(p.source)
        m = engine.match(p, default_country=spec.default_country if spec else None,
                         default_workplace=spec.default_workplace if spec else None, now=now)
        row = match_row(p, m, now)
        row["version_hash"] = r.get("version_hash")
        matches.append(row)
    store.append("matches", matches)
    store.append("rule_versions", [{"rule_version": engine.rule_version, "created_at": now,
                                    "yaml": json.dumps(engine.cfg, ensure_ascii=False, default=str)[:200_000]}])
    return len(matches)


def budget_minutes(kind: str = "daily") -> float:
    us = config.user_settings()
    env = os.environ.get("JR_BUDGET_MINUTES")
    if env:
        return float(env)
    budgets = us.get("budgets") or {}
    if kind == "ats":
        return float(budgets.get("ats_minutes", 20))
    return float(budgets.get("daily_minutes", DEFAULT_MINUTES.get(config.mode(), 15)))


def run_daily(store: Any, *, dry_run: bool = False, force: bool = False,
              only: list[str] | None = None, skip_fetch: bool = False, notify: bool | None = None,
              runner: str | None = None, llm: Any = None, minutes: float | None = None) -> dict[str, Any]:
    from jobradar.pipeline.run import plan_sources, run_sources
    from jobradar.pipeline.shortlist import build_shortlist
    from jobradar.settings import get_settings

    from jobradar.pipeline import minutes as governor

    started = time.monotonic()
    out: dict[str, Any] = {"mode": config.mode()}
    if minutes is None:
        minutes, out["actions_minutes"] = governor.scale(store, "daily", budget_minutes("daily"))
    out["budget_minutes"] = round(minutes, 1)
    # leave room for the shortlist, screen and the state push after fetching
    fetch_deadline = started + max(1.0, minutes * 0.7) * 60
    if not dry_run and notion_sync.configured():
        try:
            out["notion_pull"] = notion_sync.run_pull(store)
        except Exception as exc:  # noqa: BLE001 - Notion down: tonight's run goes on, statuses come next night
            log.warning("notion pull failed: %s", exc)
            out["notion_pull"] = {"status": "failed", "error": str(exc)[:200]}
    if not skip_fetch:
        runner = runner or get_settings().runner
        due = plan_sources(store, runner=runner, only=only)
        summaries = run_sources(due, (lambda: store) if not dry_run else None, dry_run=dry_run, trigger="daily",
                                deadline=fetch_deadline)
        ran = [s for s in summaries if s.status != "not_started"]
        out["sources"] = {"due": len(due), "ran": len(ran), "queued": len(due) - len(ran),
                          "failed": sum(s.status in ("failed", "blocked") for s in ran),
                          "fetched": sum(s.fetched for s in ran), "matched": sum(s.matched for s in ran)}
    from jobradar.pipeline.shortlist import screen_minutes

    notion_on = not dry_run and notion_sync.configured()
    end = started + float(os.environ.get("JR_RUN_MINUTES") or RUN_MINUTES) * 60
    screen_deadline = min(time.monotonic() + screen_minutes() * 60,
                          end - (NOTION_RESERVE_MINUTES * 60 if notion_on else 0))
    out["shortlist"] = build_shortlist(store, dry_run=dry_run, force=force, llm=llm, screen_deadline=screen_deadline)
    if notion_on:
        try:
            out["notion"] = notion_sync.run_push(
                store, deadline=min(time.monotonic() + notion_sync.notion_minutes() * 60, end))
        except Exception as exc:  # noqa: BLE001 - pages left now go out the next night
            log.warning("notion push failed: %s", exc)
            out["notion"] = {"status": "failed", "error": str(exc)[:200]}
        if config.module_on("screen"):
            try:
                out["filter_suggestions"] = notion_sync.run_suggest(store, llm)
            except Exception as exc:  # noqa: BLE001 - e.g. the AI quota is spent: next night
                log.warning("filter suggestions skipped: %s", exc)
    if not dry_run:
        out["upkeep"] = store.maintenance()
        if notify is None:
            notify = bool(config.user_settings().get("notify_email"))
        if notify and out["shortlist"].get("status") == "done" and out["shortlist"].get("shortlisted"):
            out["emailed"] = send_ready_mail(out["shortlist"])
    out["minutes"] = round((time.monotonic() - started) / 60, 1)
    if not dry_run and get_settings().runner == "github":
        governor.record(store, "daily", (time.monotonic() - started) / 60)
    return out


def send_ready_mail(res: dict[str, Any]) -> bool:
    """A short "N new jobs ready" e-mail (no job titles: the list lives in the app)."""
    from jobradar.digest import smtp

    n = res.get("shortlisted") or 0
    by = res.get("by_section") or {}
    parts = [f"{by[k]} {label}" for k, label in (("top", "top picks"), ("remote", "remote"), ("home", "near home"),
                                                   ("elsewhere", "elsewhere")) if by.get(k)]
    subject = f"Job Radar: {n} new job{'s' if n != 1 else ''} ready"
    text = f"{subject} ({', '.join(parts)}).\n\nOpen the app to pick: jr app\n"
    try:
        smtp.send(subject, f"<p>{text}</p>", text)
        return True
    except Exception as exc:  # noqa: BLE001 - the e-mail is a nicety, never fails the run
        log.warning("ready e-mail not sent: %s", type(exc).__name__)
        return False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
