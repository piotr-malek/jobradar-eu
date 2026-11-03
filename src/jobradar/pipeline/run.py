"""Run one source end-to-end: fetch → diff versions → match new/changed → append rows.

Shared by `jr run`, `jr probe` and `jr daily`.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from jobradar.dedup.cluster import ats_key, fz_key
from jobradar.http.client import Blocked, BudgetExceeded, PoliteClient
from jobradar.match.engine import MatchEngine
from jobradar.models import MatchResult, Posting, utcnow
from jobradar.normalize.names import norm_company
from jobradar.normalize.text import fix_mojibake
from jobradar.settings import get_settings
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter, load_specs, spec_for

log = logging.getLogger(__name__)
DESC_MAX = 20_000


def code_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("jobradar-eu")
    except PackageNotFoundError:
        return "dev"


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{utcnow():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"


@dataclass
class RunSummary:
    source_id: str
    status: str
    run_id: str
    requests: int = 0
    fetched: int = 0
    new_versions: int = 0
    title_relevant: int = 0
    matched: int = 0
    dropped: int = 0
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    samples: list[dict[str, Any]] = field(default_factory=list)
    pct_with_description: float | None = None
    median_age_days: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def version_row(p: Posting, m: MatchResult, run_id: str, now: datetime) -> dict[str, Any]:
    relevant = m.tier != "excluded"
    return {
        "posting_key": p.posting_key, "source": p.source, "source_job_id": p.source_job_id,
        "version_hash": p.version_hash(), "run_id": run_id, "observed_at": now,
        "title": p.title[:500], "company": p.company[:300], "company_norm": norm_company(p.company),
        "url": p.url, "apply_url": p.apply_url, "location_raw": p.location_raw[:1000],
        "countries": p.countries, "cities": p.cities, "workplace": p.workplace, "remote_scope": p.remote_scope,
        # Full text only for title-relevant postings: keeps storage tiny, drop audit still has titles.
        "description": p.description[:DESC_MAX] if relevant else None,
        "posted_at": p.posted_at, "employment_types": p.employment_types,
        "salary_min": p.salary_min, "salary_max": p.salary_max, "salary_currency": p.salary_currency,
        "salary_period": p.salary_period, "tags": p.tags[:30], "ats_type": p.ats_type, "ats_slug": p.ats_slug,
        "ats_job_id": p.ats_job_id, "ats_key": ats_key(p.apply_url or p.url, p.ats_type, p.ats_slug, p.ats_job_id),
        "fz_key": fz_key(p.company, p.title),
        "raw": json.dumps(p.raw, ensure_ascii=False, default=str)[:20_000] if (relevant and p.raw) else None,
    }


def match_row(p: Posting, m: MatchResult, now: datetime) -> dict[str, Any]:
    d = asdict(m)
    d.update({"version_hash": p.version_hash(), "matched_at": now, "source": p.source,
              "title": p.title[:500], "company": p.company[:300]})
    return d


def process_postings(
    postings: Iterable[Posting],
    *,
    spec: SourceSpec,
    engine: MatchEngine,
    known: dict[str, str],
    run_id: str,
    now: datetime | None = None,
) -> tuple[list[dict], list[dict], list[dict], dict[str, Any]]:
    """Return (versions, sightings, matches, stats). Only new/changed versions are re-matched."""
    now = now or utcnow()
    versions, sightings, matches = [], [], []
    seen: set[str] = set()
    stats = {"fetched": 0, "new_versions": 0, "title_relevant": 0, "matched": 0, "dropped": 0, "with_desc": 0,
             "ages": [], "samples": []}
    for p in postings:
        pk = p.posting_key
        if pk in seen:
            continue
        if spec.track and spec.track not in p.tags:
            p.tags.append(spec.track)
        seen.add(pk)
        stats["fetched"] += 1
        if p.description:
            stats["with_desc"] += 1
        if p.posted_at:
            stats["ages"].append((now - p.posted_at).total_seconds() / 86400)
        vh = p.version_hash()
        sightings.append({"posting_key": pk, "source": p.source, "run_id": run_id, "seen_at": now, "version_hash": vh})
        if known.get(pk) == vh:
            continue
        m = engine.match(p, default_country=spec.default_country, default_workplace=spec.default_workplace, now=now)
        stats["new_versions"] += 1
        stats["title_relevant"] += m.tier != "excluded"
        if m.decision == "match":
            stats["matched"] += 1
            if len(stats["samples"]) < 8:
                stats["samples"].append({"title": p.title, "company": p.company, "location": p.location_raw[:80],
                                         "section": m.section, "family": m.family, "flags": m.flags})
        else:
            stats["dropped"] += 1
        versions.append(version_row(p, m, run_id, now))
        matches.append(match_row(p, m, now))
    return versions, sightings, matches, stats


def build_client(spec: SourceSpec, *, max_requests: int | None = None, record_dir: Path | None = None,
                 sleep: bool = True) -> PoliteClient:
    pol = spec.politeness or {}
    return PoliteClient(
        spec.id,
        rps=float(pol.get("rps", 0.5)),
        max_requests=int(max_requests or pol.get("max_requests", 60)),
        browser=bool(pol.get("browser", False)),
        respect_robots=bool(pol.get("robots", False)),
        record_dir=record_dir,
        sleep=sleep,
    )


def run_source(
    source_id: str,
    store: Any,
    *,
    dry_run: bool = False,
    max_requests: int | None = None,
    record_dir: Path | None = None,
    trigger: str = "manual",
    engine: MatchEngine | None = None,
) -> RunSummary:
    settings = get_settings()
    spec = load_specs().get(source_id) or spec_for(source_id)
    engine = engine or MatchEngine.load()
    run_id = new_run_id(source_id)
    started = utcnow()
    summary = RunSummary(source_id=source_id, status="success", run_id=run_id)
    missing = [s for s in spec.secrets if not settings.secret(s)]
    if missing:
        summary.status, summary.error = "skipped_no_key", f"missing secrets: {', '.join(missing)}"
        _record_run(store, spec, summary, started, dry_run, trigger, engine)
        return summary

    known = {} if dry_run or store is None else store.known_versions(source_id)
    http = build_client(spec, max_requests=max_requests, record_dir=record_dir)
    purpose = spec.track == "purpose"
    ctx = FetchContext(spec=spec, http=http, wants_detail=lambda title: engine.wants_detail(title, purpose=purpose),
                       known=known, secrets={s: settings.secret(s) or "" for s in spec.secrets})
    postings: list[Posting] = []
    try:
        for p in get_adapter(spec.adapter)(ctx):
            for f in ("title", "company", "location_raw", "description"):
                setattr(p, f, fix_mojibake(getattr(p, f) or "") or getattr(p, f))
            postings.append(p)
    except Blocked as exc:
        summary.status, summary.error = ("partial" if postings else "blocked"), str(exc)[:500]
    except BudgetExceeded as exc:
        summary.status, summary.error = "partial", str(exc)[:300]
    except Exception as exc:  # noqa: BLE001 - one broken source never stops the run
        log.exception("source %s failed", source_id)
        summary.status, summary.error = ("partial" if postings else "failed"), f"{type(exc).__name__}: {exc}"[:500]
    finally:
        http.close()
    summary.requests = http.stats.requests
    summary.notes = ctx.notes[:10]

    now = utcnow()
    versions, sightings, matches, stats = process_postings(
        postings, spec=spec, engine=engine, known=known, run_id=run_id, now=now)
    fetched_keys = {r["posting_key"] for r in sightings}
    seen_only = [{"posting_key": pk, "source": spec.id, "run_id": run_id, "seen_at": now, "version_hash": known.get(pk)}
                 for pk in dict.fromkeys(ctx.seen_known) if pk not in fetched_keys]
    sightings += seen_only
    # records read from the source, yielded or not: a run that only re-saw stored postings or
    # pre-filtered every title away still worked, so it must not look "empty"/broken
    summary.fetched = stats["fetched"] + len(seen_only) + ctx.skipped
    summary.new_versions = stats["new_versions"]
    summary.title_relevant = stats["title_relevant"]
    summary.matched = stats["matched"]
    summary.dropped = stats["dropped"]
    summary.samples = stats["samples"]
    if stats["fetched"]:
        summary.pct_with_description = round(100 * stats["with_desc"] / stats["fetched"], 1)
    if stats["ages"]:
        summary.median_age_days = round(statistics.median(stats["ages"]), 1)
    if summary.status == "success" and summary.fetched == 0:
        summary.status = "empty"
    if not dry_run and store is not None:
        store.append("posting_versions", versions)
        store.append("posting_sightings", sightings)
        store.append("matches", matches)
    _record_run(store, spec, summary, started, dry_run, trigger, engine, http)
    return summary


FETCH_WORKERS = 4


def run_sources(source_ids: list[str], store_factory: Callable[[], Any] | None, *, dry_run: bool = False,
                trigger: str = "manual", workers: int | None = None,
                on_done: Callable[[RunSummary], None] | None = None,
                deadline: float | None = None) -> list[RunSummary]:
    """Run sources a few at a time (``JR_FETCH_WORKERS``, default 4; 1 = one after another).

    Politeness is unchanged: each source keeps its own client, rate limit and budget, and sources whose adapters live
    in the same module (often the same site or API family) run one after another in one worker, so no site ever sees
    two of our clients at once. Each worker opens its own store. Results come back in ``source_ids`` order.

    ``deadline`` (time.monotonic()): no source starts after it; those come back with status "not_started" and stay
    due for the next run (the run budget keeps GitHub Actions minutes in bounds)."""
    workers = max(1, workers or int(os.environ.get("JR_FETCH_WORKERS") or FETCH_WORKERS))
    groups: dict[str, list[str]] = {}
    specs = load_specs(all_sources=True)
    for sid in source_ids:                      # resolving adapters here also loads the registry before any thread
        groups.setdefault(get_adapter(specs[sid].adapter).__module__, []).append(sid)
    engine = MatchEngine.load()
    lock = threading.Lock()

    def run_group(ids: list[str]) -> list[RunSummary]:
        store = None if dry_run or store_factory is None else store_factory()
        out = []
        for sid in ids:
            if deadline is not None and time.monotonic() > deadline:
                out.append(RunSummary(source_id=sid, status="not_started", run_id=""))
                continue
            s = run_source(sid, store, dry_run=dry_run, trigger=trigger, engine=engine)
            out.append(s)
            if on_done:
                with lock:
                    on_done(s)
        return out

    with ThreadPoolExecutor(max_workers=min(workers, len(groups) or 1), thread_name_prefix="fetch") as pool:
        done = [s for f in [pool.submit(run_group, ids) for ids in groups.values()] for s in f.result()]
    by_id = {s.source_id: s for s in done}
    return [by_id[sid] for sid in source_ids]


def _record_run(store, spec, summary: RunSummary, started, dry_run, trigger, engine, http=None) -> None:
    if dry_run or store is None:
        return
    store.append("source_runs", [{
        "run_id": summary.run_id, "source_id": spec.id, "kind": spec.kind, "runner": get_settings().runner,
        "trigger": trigger, "started_at": started, "finished_at": utcnow(), "status": summary.status,
        "requests": summary.requests, "retries": http.stats.retries if http else 0,
        "status_counts": json.dumps(http.stats.status_counts) if http else None,
        "bytes": http.stats.bytes if http else 0, "records_fetched": summary.fetched,
        "new_versions": summary.new_versions, "sightings": summary.fetched, "matched": summary.matched,
        "dropped": summary.dropped, "error": summary.error, "rule_version": engine.rule_version,
        "code_version": code_version(),
    }])


# ------------------------------------------------------------------ planning
def plan_sources(store: Any, *, runner: str, now: datetime | None = None, only: list[str] | None = None) -> list[str]:
    """Sources due today for this runner (cadence = every_days since last successful run)."""
    now = now or utcnow()
    specs = load_specs()                         # the user's sources (countries, opt-in tracks, enable/disable)
    last = store.last_runs() if store is not None else {}
    due = []
    for sid, spec in specs.items():
        if only and sid not in only:
            continue
        if not spec.enabled or spec.kind == "ats":
            continue
        # Actions runs only github/any sources; a local run (home IP) can run everything.
        if runner != "local" and spec.runner not in ("any", runner):
            continue
        info = last.get(sid) or {}
        last_ok = info.get("last_ok")
        if isinstance(last_ok, str):
            last_ok = datetime.fromisoformat(last_ok)
        # 2h slack so a daily cron that drifts a little is still "due"
        if last_ok and now - last_ok < timedelta(days=spec.every_days) - timedelta(hours=2):
            continue
        due.append(sid)
    return due


def probe_row(summary: RunSummary, runner: str) -> dict[str, Any]:
    return {
        "probe_id": summary.run_id, "source_id": summary.source_id, "probed_at": utcnow(), "runner": runner,
        "ok": summary.status in ("success", "partial") and summary.fetched > 0, "status": summary.status,
        "requests": summary.requests, "records": summary.fetched, "title_relevant": summary.title_relevant,
        "matched": summary.matched, "pct_with_description": summary.pct_with_description,
        "median_age_days": summary.median_age_days,
        "notes": "; ".join(filter(None, [summary.error, *summary.notes]))[:1000],
    }


def utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
