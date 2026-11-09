"""The morning shortlist: new matched jobs (clustered, deduplicated against everything shown or decided before),
routed to their application form, optionally screened by an LLM, written to state.db for the app.

One shortlist per night (`nights` table).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from jobradar import config
from jobradar.dedup.cluster import cluster_rows, fz_key, short_job_id
from jobradar.match.engine import MatchEngine
from jobradar.normalize.names import clean_title, company_key, title_key
from jobradar.normalize.text import clip
from jobradar.settings import get_settings
from jobradar.sources.registry import load_specs

log = logging.getLogger(__name__)

SECTIONS = [("top", "Top picks"), ("remote", "Remote"), ("home", "Near home"), ("elsewhere", "Elsewhere"),
            ("review", "Worth a look")]
ROUTE_FIELDS = ("source", "url", "apply_url", "ats_type", "ats_slug", "ats_job_id")
ATS_SOURCES = ("greenhouse", "lever", "ashby", "workable", "recruitee", "personio", "smartrecruiters", "teamtailor",
               "bamboohr", "breezy", "jazzhr", "workday", "softgarden", "homerun", "join", "traffit")
PER_COMPANY_CAP = 3
SCREEN_MINUTES = 8         # per night; at Gemini's free 10 requests a minute, ~800 jobs
SCREEN_DAYS = 30           # older unscreened jobs have lost their description (store maintenance)
DESCRIBE_MINUTES = 4       # the description finder's share of the night
LOW_FIT = 2                # screened fit at or below this is hidden (kept in state.db with hidden="low_fit")
ACTED_ON = ("picked", "applied", "screening", "interview", "offer", "rejected")
SAME_COMPANY_MAX = 3


@dataclass
class Item:
    job_id: str
    cluster_id: str
    section: str
    family: str
    family_label: str
    title: str
    company: str
    location_display: str
    work_mode: str
    countries: list[str]
    url: str
    posting_url: str
    sources: list[str]
    priority: float
    flags: list[str]
    stack_hits: list[str]
    seniority: str | None
    salary: str | None
    posted_at: str | None
    age_days: float | None
    member_posting_keys: list[str]
    dedup_keys: list[str]
    representative_posting_key: str
    description: str = ""
    routes: list[dict[str, Any]] = field(default_factory=list)
    copies: list[dict[str, Any]] = field(default_factory=list)
    why: dict[str, Any] = field(default_factory=dict)
    in_body: bool = True
    rank: int = 0


def _dt(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def fmt_salary(r: dict[str, Any]) -> str | None:
    lo, hi, cur, per = r.get("salary_min"), r.get("salary_max"), r.get("salary_currency") or "", r.get("salary_period") or ""
    if not lo and not hi:
        return None

    def k(v: float) -> str:
        return f"{v / 1000:.0f}k" if v >= 10_000 else f"{v:,.0f}"

    rng = f"{k(lo)}–{k(hi)}" if lo and hi and lo != hi else k(lo or hi)
    per_s = {"year": "/yr", "month": "/mo", "day": "/day", "hour": "/h"}.get(per.lower(), "")
    return f"{cur} {rng}{per_s}".strip()


def location_display(r: dict[str, Any]) -> str:
    mode = r.get("work_mode") or "unknown"
    loc = clip((r.get("location_raw") or "").replace("\n", " "), 60)
    countries = ",".join(r.get("countries") or [])
    mode_s = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site", "unknown": ""}.get(mode, "")
    return " · ".join(p for p in (mode_s, loc or countries) if p)


def listing_copies(members: list[dict[str, Any]], rep: dict[str, Any], limit: int = 25) -> list[dict[str, Any]]:
    """The cluster's other listings, one per link (a copy per city, an agency's title on several boards)."""
    seen = {rep.get("url") or rep.get("apply_url")}
    out = []
    for m in sorted(members, key=lambda r: (r.get("_rank", 50), str(r.get("first_seen_at") or ""))):
        link = m.get("url") or m.get("apply_url")
        if not link or link in seen:
            continue
        seen.add(link)
        out.append({"title": clean_title(m.get("title")), "company": m.get("company") or "",
                    "location": location_display(m), "source": m["source"], "url": link})
    return out[:limit]


def why_matched(rep: dict[str, Any], engine: MatchEngine) -> dict[str, Any]:
    """The `jr explain` facts the app shows: title rule, family, setup, stack hits, flags."""
    ex = rep.get("explain")
    if isinstance(ex, str):
        try:
            ex = json.loads(ex)
        except ValueError:
            ex = {}
    ex = ex or {}
    return {"rule": (ex.get("title") or {}).get("rule"), "family": engine.family_label(rep.get("family")) or rep.get("family"),
            "mode": (ex.get("location") or {}).get("mode"), "mode_source": (ex.get("location") or {}).get("mode_source"),
            "stack": rep.get("stack_hits") or [], "stack_score": ex.get("stack_score"), "flags": rep.get("flags") or []}


def select_items(store: Any, engine: MatchEngine, *, since: datetime, now: datetime) -> tuple[list[Item], dict[str, Any]]:
    rows = store.candidates(since, engine.rule_version)
    stats: dict[str, Any] = {"candidates": len(rows)}
    active_cut = now - timedelta(days=10)
    rows = [r for r in rows if (_dt(r.get("last_seen_at")) or now) >= active_cut]
    stats["active"] = len(rows)
    ranks = {sid: s.rank for sid, s in load_specs(all_sources=True).items()}
    ranks.update({f"ats_{v}": 10 for v in ATS_SOURCES})
    clusters = cluster_rows(rows, ranks)
    sent = store.sent_dedup_keys()
    stats["clusters"] = len(clusters)
    elsewhere = {f"fz:{k}" for r in store.elsewhere_dropped(since, engine.rule_version)
                 if (k := fz_key(r.get("company"), r.get("title")))}
    items: list[Item] = []
    already = copies = 0
    for c in clusters:
        if c.keys & sent or f"job:{short_job_id(c.cluster_id)}" in sent:
            already += 1
            continue
        if c.keys & elsewhere and all(engine.unstated_elsewhere(m) for m in c.members):
            copies += 1                 # a board left the mode out; another says hybrid/on-site outside the area
            continue
        rep = c.representative
        best = max(c.members, key=lambda m: m.get("priority") or 0)
        family = rep.get("family") or best.get("family") or "review"
        flags = sorted({f for m in c.members for f in (m.get("flags") or [])})
        section = "review" if family == "review" else (rep.get("section") or best.get("section"))
        if "top_pick" in flags and section != "review":
            section = "top"
        posted = min((d for d in (_dt(m.get("posted_at")) for m in c.members) if d), default=None)
        age = (now - posted).total_seconds() / 86400 if posted else None
        items.append(Item(
            job_id=short_job_id(c.cluster_id), cluster_id=c.cluster_id, section=section, family=family,
            family_label=engine.family_label(family), title=clean_title(rep.get("title")),
            company=rep.get("company") or "", location_display=location_display(rep),
            work_mode=rep.get("work_mode") or "unknown", countries=list(rep.get("countries") or []),
            url=rep.get("apply_url") or rep.get("url") or "", posting_url=rep.get("url") or rep.get("apply_url") or "",
            sources=c.sources, priority=max(m.get("priority") or 0 for m in c.members), flags=flags,
            stack_hits=sorted({s for m in c.members for s in (m.get("stack_hits") or [])}),
            seniority=rep.get("seniority"), salary=next((fmt_salary(m) for m in c.members if fmt_salary(m)), None),
            posted_at=posted.isoformat() if posted else None, age_days=round(age, 1) if age is not None else None,
            member_posting_keys=sorted(m["posting_key"] for m in c.members), dedup_keys=sorted(c.keys),
            representative_posting_key=rep["posting_key"],
            description=max((m.get("description") or "" for m in c.members), key=len),
            routes=[{k: m.get(k) for k in ROUTE_FIELDS} for m in c.members], copies=listing_copies(c.members, rep),
            why=why_matched(rep, engine),
        ))
    stats["already_shown"] = already
    stats["elsewhere_copies"] = copies
    order = {s: i for i, (s, _) in enumerate(SECTIONS)}
    items.sort(key=lambda it: (order.get(it.section, 9), -it.priority, it.age_days if it.age_days is not None else 99))
    per_company: Counter[str] = Counter()
    for i, it in enumerate(items):
        it.rank = i + 1
        per_company[company_key(it.company)] += 1
        it.in_body = per_company[company_key(it.company)] <= PER_COMPANY_CAP
    stats["items"] = len(items)
    stats["by_section"] = dict(Counter(it.section for it in items))
    return items, stats


class CompanyIndex:
    """What the user already saw or did at a company: shown in the app as "Same company"."""

    def __init__(self, store: Any):
        decided = store.decisions_current()
        self.by_company: dict[str, list[dict[str, Any]]] = {}
        for r in store.query("SELECT job_id, title, company FROM shortlist GROUP BY job_id"):
            d = decided.get(r["job_id"]) or {}
            self.add(r["company"] or "", r["title"] or "", d.get("status") or "new", r["job_id"])
        for jid, d in decided.items():               # added by hand in the app (M- ids), never shortlisted
            if jid.startswith("M-"):
                self.add(d.get("company") or "", d.get("title") or "", d.get("status") or "new", jid)

    def add(self, company: str, title: str, status: str, job_id: str) -> None:
        if ck := company_key(company):
            self.by_company.setdefault(ck, []).append(
                {"title": title, "tkey": title_key(title), "status": status, "job_id": job_id})

    def same_company(self, company: str, title: str, exclude: str = "") -> str:
        tkey = title_key(title)
        rows = [p for p in self.by_company.get(company_key(company)) or [] if p["job_id"] != exclude]
        hits = [p for p in rows if p["status"] in ACTED_ON]
        hits += [p for p in rows if p["status"] not in ACTED_ON and _similar_titles(tkey, p["tkey"])]
        out = [f"{p['status']}: {p['title']} ({p['job_id']})" for p in hits[:SAME_COMPANY_MAX]]
        if len(hits) > SAME_COMPANY_MAX:
            out.append(f"+{len(hits) - SAME_COMPANY_MAX} more")
        return " · ".join(out)


def _similar_titles(a: str, b: str) -> bool:
    x, y = set(a.split()), set(b.split())
    if not x or not y:
        return False
    return (min(len(x), len(y)) >= 2 and (x <= y or y <= x)) or len(x & y) / len(x | y) >= 0.5


def local_today(now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(get_settings().timezone)).date()


def build_shortlist(store: Any, *, kind: str = "daily", dry_run: bool = False, force: bool = False,
                    backlog_days: int | None = None, llm: Any = None, http: Any = None,
                    now: datetime | None = None, engine: MatchEngine | None = None,
                    screen_deadline: float | None = None) -> dict[str, Any]:
    """Select → route → screen (when switched on) → shortlist rows + a nights row. One per night unless forced."""
    from jobradar.apply.route import resolve, route_for_members
    from jobradar.pipeline.daily import ensure_rule_version

    now = now or datetime.now(timezone.utc)
    engine = engine or MatchEngine.load()
    night = local_today(now)
    if not dry_run and not force and store.night_done(night, kind):
        return {"status": "already_done", "night": night.isoformat(), "kind": kind}
    rematched = 0 if dry_run else ensure_rule_version(store, engine)
    window = backlog_days or int(os.environ.get("JR_LOOKBACK_DAYS") or 4)
    items, stats = select_items(store, engine, since=now - timedelta(days=window), now=now)
    cap = int(os.environ.get("JR_SHORTLIST_CAP") or config.user_settings().get("shortlist_cap") or 0)
    todo, deferred = (items[:cap], items[cap:]) if cap else (items, [])
    routes = {it.job_id: route_for_members(it.routes) for it in todo}
    base = {"night": night.isoformat(), "kind": kind, "rematched": rematched,
            **{k: v for k, v in stats.items() if k != "by_section"}, "by_section": stats.get("by_section")}
    if dry_run:
        return {**base, "status": "dry_run", "would_shortlist": len(todo), "over_cap": len(deferred),
                "preview": [{"job_id": it.job_id, "section": it.section, "family": it.family, "title": it.title,
                             "company": it.company, "location": it.location_display,
                             "route": routes[it.job_id].vendor} for it in todo[:50]]}

    unknown = [jid for jid, r in routes.items() if r.vendor == "unknown" and r.url.startswith("http")]
    resolve_cap = int(os.environ.get("JR_RESOLVE_CAP") or 60)
    if unknown and resolve_cap:
        from jobradar.http.client import PoliteClient

        own = http is None
        http = http or PoliteClient("route_resolve", rps=0.5, max_requests=resolve_cap * 2, respect_robots=True)
        try:
            for jid in unknown[:resolve_cap]:
                routes[jid] = resolve(routes[jid], http)
        finally:
            if own:
                http.close()

    from jobradar.pipeline.describe import describe

    base["descriptions"] = describe(store, todo, routes, deadline=time.monotonic() + DESCRIBE_MINUTES * 60)
    index = CompanyIndex(store)
    night_id = f"{night.isoformat()}-{kind}-{uuid.uuid4().hex[:6]}"
    rows = []
    for it in todo:
        r = routes[it.job_id]
        row = asdict(it)
        row.update({
            "night_id": night_id, "night": night, "description": clip(it.description, 20_000),
            "copies": json.dumps(it.copies, ensure_ascii=False), "routes": json.dumps(it.routes, ensure_ascii=False),
            "route_vendor": r.vendor, "route_url": r.url or it.url, "readable": r.readable, "effort_min": r.effort_min,
            "watch_out": [], "instructions": [], "why": json.dumps(it.why, ensure_ascii=False),
            "same_company": index.same_company(it.company, it.title),
        })
        index.add(it.company, it.title, "new", it.job_id)
        rows.append(row)
    store.append("shortlist", rows)
    sq: dict[str, Any] = {"screened": 0, "hidden_low_fit": 0, "screen_left": 0}
    if config.module_on("screen"):
        sq = screen_queue(store, llm=llm, deadline=screen_deadline, now=now)
    hidden = sum(1 for r in store.query("SELECT hidden FROM shortlist WHERE night_id = ?", (night_id,))
                 if r["hidden"] == "low_fit")
    status = "done"
    store.append("nights", [{"night_id": night_id, "night": night, "kind": kind, "status": status,
                             "finished_at": datetime.now(timezone.utc), "n_candidates": stats.get("candidates"),
                             "n_items": len(rows), "n_screened": sq["screened"], "rule_version": engine.rule_version,
                             "stats": json.dumps({**base, "hidden": hidden, "deferred": len(deferred), **sq},
                                                 default=str)}])
    return {**base, "status": status, "shortlisted": len(rows) - hidden, "hidden_low_fit": hidden,
            "screened": sq["screened"], "screen_left": sq["screen_left"], "deferred": len(deferred),
            "by_route": dict(Counter(r.vendor for r in routes.values()))}


def screen_minutes() -> float:
    us = config.user_settings()
    return float(os.environ.get("JR_SCREEN_MINUTES") or (us.get("budgets") or {}).get("screen_minutes")
                 or SCREEN_MINUTES)


def screen_queue(store: Any, *, llm: Any = None, deadline: float | None = None,
                 now: datetime | None = None) -> dict[str, Any]:
    """Screen shortlisted jobs that have no screen result yet, newest night first, until the provider chain's
    quota or `deadline` runs out; the rest wait for the next night. Skipped jobs are left alone."""
    from jobradar.apply.screen import screen_chain

    now = now or datetime.now(timezone.utc)
    if deadline is None:
        deadline = time.monotonic() + screen_minutes() * 60
    since = (now - timedelta(days=SCREEN_DAYS)).date()
    rows = store.query(
        """SELECT s.night_id, s.job_id, s.title, s.company, s.location_display, s.description
           FROM shortlist s LEFT JOIN v_decisions_current d USING (job_id)
           WHERE s.screened_at IS NULL AND s.night >= :since AND COALESCE(d.status, 'new') != 'skipped'
           ORDER BY s.night DESC, s.rank""", {"since": since})
    todo: dict[str, dict[str, Any]] = {}
    for r in rows:
        todo.setdefault(r["job_id"], r)
    jobs = [{"job_id": r["job_id"], "title": r["title"], "company": r["company"],
             "location": r["location_display"] or "", "description": r["description"] or ""} for r in todo.values()]
    res = screen_chain(jobs, deadline=deadline, client=llm) if jobs else {}
    hide_at = int(config.user_settings().get("hide_fit_at_or_below", LOW_FIT))
    hidden = 0
    for job_id, sc in res.items():
        low = sc.get("fit") is not None and sc["fit"] <= hide_at
        hidden += low
        store.execute(
            """UPDATE shortlist SET fit = :fit, summary = :summary, watch_out = :watch_out,
                 instructions = :instructions, work_setup = :work_setup, screen_salary = :salary,
                 languages_required = :languages_required, years_required = :years_required,
                 contract = :contract, apply_deadline = :deadline, screen_model = :model, screened_at = :at,
                 hidden = CASE WHEN :low THEN 'low_fit' ELSE hidden END
               WHERE job_id = :job_id AND night_id = :night_id""",
            {"fit": sc.get("fit"), "summary": sc.get("summary"),
             "watch_out": json.dumps(sc.get("watch_out") or [], ensure_ascii=False),
             "instructions": json.dumps(sc.get("instructions") or [], ensure_ascii=False),
             "work_setup": sc.get("work_setup"), "salary": sc.get("salary"),
             "languages_required": sc.get("languages_required"), "years_required": sc.get("years_required"),
             "contract": sc.get("contract"), "deadline": sc.get("deadline"), "model": sc.get("model"),
             "at": datetime.now(timezone.utc), "low": int(low), "job_id": job_id,
             "night_id": todo[job_id]["night_id"]})
    return {"screened": len(res), "hidden_low_fit": hidden, "screen_left": len(jobs) - len(res),
            "screen_models": dict(Counter(sc.get("model") for sc in res.values()))}
