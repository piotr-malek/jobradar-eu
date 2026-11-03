"""`jr weekly`: applications per week and reply rates by source, family and route, from the user's decisions
(the `picks` branch, imported into state.db). Also compacts state.db (VACUUM). Shown in the app; optionally e-mailed.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from jobradar import config

REPLY = ("screening", "interview", "offer", "rejected")
APPLIED = ("applied", *REPLY)


def _week(at: Any) -> str:
    d = datetime.fromisoformat(str(at).replace("Z", "+00:00")).date() if not isinstance(at, date) else at
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def weekly_stats(store: Any, *, weeks: int = 12, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    decisions = store.query("SELECT job_id, status, at, family, source, route FROM decisions ORDER BY at")
    jobs = {r["job_id"]: r for r in store.query(
        "SELECT job_id, family, sources, route_vendor, fit, night FROM shortlist GROUP BY job_id", table="shortlist")}
    applied_at: dict[str, str] = {}
    furthest: dict[str, str] = {}
    reached: dict[str, set[str]] = defaultdict(set)
    picked = skipped = 0
    for d in decisions:
        st = d["status"]
        picked += st == "picked"
        skipped += st == "skipped"
        reached[d["job_id"]].add(st)
        if st in APPLIED and d["job_id"] not in applied_at:
            applied_at[d["job_id"]] = d["at"]
        if st in APPLIED and APPLIED.index(st) >= APPLIED.index(furthest.get(d["job_id"], "applied")):
            furthest[d["job_id"]] = st
    cutoff = (now - timedelta(weeks=weeks)).date().isoformat()
    per_week: Counter[str] = Counter(_week(at) for jid, at in applied_at.items() if str(at)[:10] >= cutoff)
    by: dict[str, dict[str, Counter[str]]] = {k: defaultdict(Counter) for k in ("source", "family", "route")}
    for jid in applied_at:
        job = jobs.get(jid) or {}
        dec = next((d for d in decisions if d["job_id"] == jid), {})
        keys = {"source": ((job.get("sources") or [None])[0]) or dec.get("source") or "added by hand",
                "family": job.get("family") or dec.get("family") or "-",
                "route": job.get("route_vendor") or dec.get("route") or "-"}
        replied = furthest.get(jid) in REPLY
        for dim, val in keys.items():
            by[dim][val]["applied"] += 1
            by[dim][val]["replied"] += replied
            by[dim][val]["interview"] += bool(reached[jid] & {"interview", "offer"})
    rates = {dim: sorted(({"name": k, **v, "reply_rate": round(v["replied"] / v["applied"], 2)}
                          for k, v in vals.items()), key=lambda r: -r["applied"]) for dim, vals in by.items()}
    return {"weeks": dict(sorted(per_week.items())), "applied_total": len(applied_at),
            "replied_total": sum(s in REPLY for s in furthest.values()),
            "interviews_total": sum(bool(reached[j] & {"interview", "offer"}) for j in applied_at),
            "offers_total": sum("offer" in reached[j] for j in applied_at),
            "picked_total": picked, "skipped_total": skipped, "by": rates}


def run_weekly(store: Any, *, dry_run: bool = False, email: bool | None = None) -> dict[str, Any]:
    stats = weekly_stats(store)
    if not dry_run:
        stats["upkeep"] = store.maintenance(vacuum=True)
        if email is None:
            email = bool(config.user_settings().get("weekly_email"))
        if email:
            stats["emailed"] = send_weekly(stats)
    return stats


def weekly_text(stats: dict[str, Any]) -> str:
    lines = [f"Applied {stats['applied_total']} · replies {stats['replied_total']} · interviews "
             f"{stats['interviews_total']} · offers {stats['offers_total']}", "", "Applications per week:"]
    lines += [f"  {w}: {n}" for w, n in list(stats["weeks"].items())[-8:]] or ["  (none yet)"]
    for dim in ("source", "family", "route"):
        rows = stats["by"].get(dim) or []
        if rows:
            lines += ["", f"Reply rate by {dim}:"]
            lines += [f"  {r['name']}: {r['replied']}/{r['applied']} ({int(r['reply_rate'] * 100)}%)" for r in rows[:8]]
    return "\n".join(lines) + "\n"


def send_weekly(stats: dict[str, Any]) -> bool:
    from jobradar.digest import smtp

    text = weekly_text(stats)
    try:
        smtp.send("Job Radar: your week", f"<pre>{text}</pre>", text)
        return True
    except Exception:  # noqa: BLE001
        return False


def dumps(stats: dict[str, Any]) -> str:
    return json.dumps(stats, indent=1, default=str)
