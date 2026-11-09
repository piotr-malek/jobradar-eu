"""GitHub Actions minutes: a private repo gets 2,000 free minutes a month. Each overnight job records how long it
ran; when this month's projection passes the limit the budgets shrink, company career pages first (down to the
smaller slice), then the job-board fetch. The AI review and the Notion writes are never cut."""

from __future__ import annotations

import calendar
import os
from datetime import datetime, timezone
from typing import Any

FREE_MINUTES = 2000
SOFT_LIMIT = 1800            # leave room for manual runs and setup
OVERHEAD = 2.0               # minutes per job GitHub bills beyond our own clock: runner start, pip install, state push


def record(store: Any, kind: str, minutes: float, *, now: datetime | None = None) -> None:
    store.append("run_minutes", [{"kind": kind, "at": now or datetime.now(timezone.utc),
                                  "minutes": round(minutes + OVERHEAD, 1)}])


def projection(store: Any, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = store.query("SELECT minutes FROM run_minutes WHERE at >= :start", {"start": start})
    used = sum(float(r["minutes"] or 0) for r in rows)
    days = calendar.monthrange(now.year, now.month)[1]
    elapsed = max(1.0, (now - start).total_seconds() / 86400)
    projected = used / elapsed * days
    limit = float(os.environ.get("JR_MINUTES_LIMIT") or SOFT_LIMIT)
    return {"used": round(used), "projected": round(projected), "limit": limit,
            "tight": projected > limit, "ratio": round(min(1.0, limit / projected), 2) if projected else 1.0}


def scale(store: Any, kind: str, minutes: float) -> tuple[float, dict[str, Any]]:
    """The night's budget for this job after the governor: ATS shrinks first, the fetch only when ATS alone can't
    bring the month back under the limit."""
    p = projection(store)
    if not p["tight"]:
        return minutes, p
    if kind == "ats":
        return max(5.0, minutes * p["ratio"] * 0.5), p
    return max(8.0, minutes * max(0.6, p["ratio"])), p
