"""Company ATS boards (Full mode): the shipped registry, tiered polling, the board → ATS feedback loop.

The package ships a pruned registry (defaults/ats_registry.csv.gz: boards that answered and had EU-eligible jobs).
`ensure_registry` merges a new registry version into state.db on upgrade without touching poll history.

Tiers (from this user's own polls; before the first poll the shipped tier decides the order):
  A  board had a posting matching the user's roles → every 2 days
  B  board had any EU-eligible posting             → every 4 days
  C  everything else                               → every 30 days
  new boards are polled at most `sweep_budget` per night
  dead after 3 consecutive 404s, a 404 on a board that never answered 200, or a successful poll with no
  EU-eligible posting (maintainer and the user's own boards excepted).

Lite mode can switch on a small slice (user settings: modules.ats_lite_tier_a): the 300 boards with most jobs in
the user's countries (registry ``countries`` counts), topped up with tier A boards hiring remotely in Europe.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from jobradar import config
from jobradar.http.client import Blocked, BudgetExceeded, PoliteClient
from jobradar.match.engine import MatchEngine
from jobradar.models import utcnow
from jobradar.normalize.location import classify_location
from jobradar.pipeline.run import code_version, new_run_id, process_postings
from jobradar.settings import PACKAGE_DIR, get_settings, user_dir
from jobradar.sources.ats.vendors import FETCHERS
from jobradar.sources.base import SourceSpec

log = logging.getLogger(__name__)

TIER_DAYS = {"A": 2, "B": 4, "C": 30}
KEEP_ORIGINS = ("maintainer", "user")      # hand-picked boards are never dropped for having no EU jobs
ORIGIN_ORDER = {"user": 0, "maintainer": 1, "board_feedback": 2, "commoncrawl": 3, "open_jobs_data": 4, "lastround": 5}
EU_VENDORS = ("personio", "recruitee", "teamtailor", "workable", "smartrecruiters", "softgarden", "traffit", "homerun",
              "join")
VENDOR_RPS = {"greenhouse": 2.0, "lever": 1.0, "ashby": 1.0, "smartrecruiters": 1.0, "workday": 0.5, "join": 0.5}
REGISTRY_FILE = PACKAGE_DIR / "ats_registry.csv.gz"
LITE_TIER_A_MAX = 300


# ------------------------------------------------------------------ registry
def shipped_registry() -> tuple[str, list[dict[str, Any]]]:
    """(version, rows) of the package's registry file."""
    with gzip.open(REGISTRY_FILE, "rt", encoding="utf-8") as fh:
        text = fh.read()
    first, _, rest = text.partition("\n")
    m = re.search(r"registry v([\w.]+)", first)
    version = m.group(1) if m else "0"
    rows = list(csv.DictReader(io.StringIO(rest if first.startswith("#") else text)))
    return version, rows


def ensure_registry(store: Any) -> dict[str, Any]:
    """Load the shipped registry into state.db once per registry version; new boards are added, known ones keep
    their poll history (polls are keyed by vendor + slug)."""
    version, rows = shipped_registry()
    origin = f"package:{version}"
    have = store.ats_registry()
    if any(r.get("origin") == origin or (r.get("extra") or "").find(f'"registry": "{version}"') >= 0 for r in have):
        return {"version": version, "added": 0}
    known = {(r["ats_type"], r["ats_slug"]): r.get("origin") for r in have}
    now = utcnow()
    fresh = []
    for r in rows:
        key = (r["vendor"], r["slug"])
        if r["vendor"] not in FETCHERS or known.get(key) == "user":
            continue
        extra = json.loads(r["extra"]) if r.get("extra") else {}
        extra.update({"company_name": r.get("company_name") or None, "shipped_tier": r.get("tier"),
                      "registry": version, "source": r.get("origin")})
        if r.get("country_hint"):
            extra["country_hint"] = r["country_hint"].split()
        if r.get("countries"):
            extra["countries"] = {cc: int(n) for cc, _, n in (x.partition(":") for x in r["countries"].split())}
        fresh.append({"ats_type": r["vendor"], "ats_slug": r["slug"], "company_name": r.get("company_name"),
                      "extra": json.dumps(extra, ensure_ascii=False), "origin": r.get("origin") or "package",
                      "discovered_at": now})
    store.append("ats_registry", fresh)
    store.append("ats_registry", [{"ats_type": "none", "ats_slug": f"registry-{version}", "origin": origin,
                                   "extra": "{}", "discovered_at": now}])
    return {"version": version, "added": len(fresh)}


def add_user_boards(store: Any, boards: list[dict[str, Any]]) -> int:
    """Boards the user added (user/companies.yaml or `jr ats-add`): never dropped for lacking EU jobs."""
    now = utcnow()
    rows = [{"ats_type": b["vendor"], "ats_slug": b["slug"], "company_name": b.get("company_name"),
             "extra": json.dumps({k: v for k, v in b.items() if k not in ("vendor", "slug")}, ensure_ascii=False),
             "origin": "user", "discovered_at": now} for b in boards if b.get("vendor") in FETCHERS and b.get("slug")]
    return store.append("ats_registry", rows)


# ------------------------------------------------------------------ plan
def board_tier(summary: dict[str, Any] | None, origin: str | None = None) -> str:
    if not summary or not summary.get("last_polled_at"):
        return "new"
    if (summary.get("consecutive_404") or 0) >= 3:
        return "dead"
    if summary.get("last_status") == 404 and not summary.get("ok_polls"):
        return "dead"                      # slug never worked (e.g. a Common Crawl hit that isn't a live board)
    if (summary.get("max_matched") or 0) > 0:
        return "A"
    if (summary.get("max_eu") or 0) > 0:
        return "B"
    if (summary.get("ok_polls") or 0) > 0 and origin not in KEEP_ORIGINS:
        return "dead"
    return "C"


def plan_boards(store: Any, *, max_boards: int = 1200, sweep_budget: int = 1500,
                now: datetime | None = None, vendors: list[str] | None = None,
                lite: bool = False) -> list[dict[str, Any]]:
    """Boards due tonight, best tiers first. `lite`: shipped tier A only, at most LITE_TIER_A_MAX."""
    now = now or utcnow()
    registry = [r for r in store.ats_registry() if r["ats_type"] != "none"]
    if lite:
        registry = lite_boards(registry)
        max_boards = min(max_boards, LITE_TIER_A_MAX)
    polls = store.ats_poll_summary()
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in registry:
        if vendors and r["ats_type"] not in vendors:
            continue
        if r["ats_type"] not in FETCHERS:
            continue
        s = polls.get((r["ats_type"], r["ats_slug"]))
        tier = board_tier(s, r.get("origin"))
        if tier == "dead":
            continue
        if tier != "new":
            last = s["last_polled_at"]
            if isinstance(last, str):
                last = datetime.fromisoformat(last)
            if now - last < timedelta(days=TIER_DAYS[tier]) - timedelta(hours=3):
                continue
        r = dict(r)
        r["tier"] = tier
        buckets[tier].append(r)
    countries = _home_countries()
    buckets["new"].sort(key=lambda r: _sweep_key(r, countries))
    plan = buckets["A"] + buckets["B"] + buckets["new"][:sweep_budget] + buckets["C"]
    return plan[:max_boards]


def lite_boards(registry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lite's fixed slice: the user's own boards, then the boards with most jobs in the user's countries, then
    shipped tier A boards with remote EU jobs; at most LITE_TIER_A_MAX."""
    countries = _home_countries()
    own = [r for r in registry if r.get("origin") == "user"]
    rest = [r for r in registry if r.get("origin") != "user"]
    local = sorted((r for r in rest if local_jobs(r, countries)), key=lambda r: -local_jobs(r, countries))
    remote = sorted((r for r in rest if not local_jobs(r, countries) and _extra(r).get("shipped_tier") == "A"),
                    key=lambda r: -int((_extra(r).get("countries") or {}).get("EU", 0)))
    return (own + local + remote)[:LITE_TIER_A_MAX]


def _extra(r: dict[str, Any]) -> dict[str, Any]:
    extra = r.get("extra") or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra or "{}")
        except ValueError:
            extra = {}
    return extra


def _home_countries() -> set[str]:
    us = config.user_sources()
    home = ((config.profile().get("location") or {}).get("country") or "").upper()
    return {c.upper() for c in us.get("countries") or []} | ({home} if home else set())


def local_jobs(r: dict[str, Any], countries: set[str] | None = None) -> int:
    """Jobs the board was seen with in the user's countries (registry counts; a bare country hint counts as 1)."""
    countries = _home_countries() if countries is None else countries
    extra = _extra(r)
    seen = extra.get("countries") or {}
    n = sum(int(seen.get(c, 0)) for c in countries)
    return n or int(bool(set(extra.get("country_hint") or []) & countries))


def _sweep_key(r: dict[str, Any], countries: set[str] | None = None) -> tuple[int, int, int, int, int]:
    """Never-polled boards: boards seen hiring in the user's countries first (most jobs first), then shipped tier A,
    then origin, EU vendors first."""
    extra = _extra(r)
    local = local_jobs(r, countries)
    tier = {"A": 0, "B": 1}.get(extra.get("shipped_tier"), 2)
    eu = r["ats_type"] in EU_VENDORS or extra.get("region") == "eu"
    return 0 if local else 1, tier, -local, ORIGIN_ORDER.get(r.get("origin"), 6), 0 if eu else 1


# ------------------------------------------------------------------ poll
def poll_boards(store: Any, boards: list[dict[str, Any]], *, engine: MatchEngine | None = None,
                dry_run: bool = False, max_requests_per_vendor: int = 3000,
                deadline: float | None = None) -> dict[str, Any]:
    """Poll boards vendor by vendor. `deadline` (time.monotonic()): stop starting boards after it; the rest stay due."""
    engine = engine or MatchEngine.load()
    by_vendor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for b in boards:
        by_vendor[b["ats_type"]].append(b)
    totals: dict[str, Any] = {"boards": 0, "postings": 0, "matched": 0, "errors": 0, "vendors": {},
                              "stopped_at_budget": False}
    for vendor, items in by_vendor.items():
        source_id = f"ats_{vendor}"
        spec = SourceSpec(id=source_id, adapter=vendor, kind="ats", rank=10)
        run_id = new_run_id(source_id)
        started = utcnow()
        known = {} if dry_run else store.known_versions(source_id)
        all_versions, all_sightings, all_matches, poll_rows = [], [], [], []
        vstats = {"boards": 0, "postings": 0, "matched": 0, "errors": 0, "requests": 0}
        status = "success"
        with PoliteClient(source_id, rps=VENDOR_RPS.get(vendor, 1.0), max_requests=max_requests_per_vendor) as http:
            for b in items:
                if deadline is not None and time.monotonic() > deadline:
                    totals["stopped_at_budget"] = True
                    break
                extra = _extra(b)
                extra.setdefault("company_name", b.get("company_name"))
                purpose = extra.get("track") == "purpose"
                err = None
                try:
                    code, postings = FETCHERS[vendor](http, b["ats_slug"], extra,
                                                      lambda title: engine.wants_detail(title, purpose=purpose))
                except BudgetExceeded as exc:
                    status, err = "partial", str(exc)
                    code, postings = 0, []
                except Blocked as exc:
                    code, postings, err = exc.status or 403, [], str(exc)[:300]
                    vstats["errors"] += 1
                except Exception as exc:  # noqa: BLE001
                    code, postings, err = 0, [], f"{type(exc).__name__}: {exc}"[:300]
                    vstats["errors"] += 1
                n_eu = n_title = n_match = 0
                for p in postings:
                    if extra.get("track") and extra["track"] not in p.tags:
                        p.tags.append(extra["track"])
                    title_ok = engine.title_verdict(p.title, purpose=purpose).tier != "excluded"
                    loc = classify_location(workplace=p.workplace, title=p.title, location_raw=p.location_raw,
                                            remote_scope=p.remote_scope, countries_structured=p.countries,
                                            description="")
                    n_title += title_ok
                    n_eu += loc.section is not None
                    n_match += title_ok and loc.section is not None
                v, s, m, st = process_postings(postings, spec=spec, engine=engine, known=known, run_id=run_id)
                all_versions += v
                all_sightings += s
                all_matches += m
                vstats["boards"] += 1
                vstats["postings"] += len(postings)
                vstats["matched"] += st["matched"]
                poll_rows.append({"ats_type": vendor, "ats_slug": b["ats_slug"], "run_id": run_id,
                                  "polled_at": utcnow(), "http_status": code, "n_jobs": len(postings), "n_eu": n_eu,
                                  "n_title_ok": n_title, "n_matched": n_match, "error": err})
                if err and "budget" in err:
                    break
            vstats["requests"] = http.stats.requests
        if not dry_run:
            store.append("posting_versions", all_versions)
            store.append("posting_sightings", all_sightings)
            store.append("matches", all_matches)
            store.append("ats_poll_results", poll_rows)
            store.append("source_runs", [{
                "run_id": run_id, "source_id": source_id, "kind": "ats", "runner": get_settings().runner,
                "trigger": "ats_poll", "started_at": started, "finished_at": utcnow(), "status": status,
                "requests": vstats["requests"], "records_fetched": vstats["postings"],
                "new_versions": len(all_versions), "sightings": len(all_sightings), "matched": vstats["matched"],
                "dropped": len(all_matches) - vstats["matched"], "error": None,
                "rule_version": engine.rule_version, "code_version": code_version(),
            }])
        totals["vendors"][vendor] = vstats
        for k in ("boards", "postings", "matched", "errors"):
            totals[k] += vstats[k]
    return totals


# ------------------------------------------------------------------ resolve
def slug_candidates(name: str) -> list[str]:
    base = re.sub(r"\b(?:sp\.? z o\.?o\.?|gmbh|ltd|inc|s\.a\.|ag|bv|technology|technologies|poland|polska)\b", "",
                  name.lower())
    words = re.findall(r"[a-z0-9]+", base)
    cands = ["".join(words), "-".join(words)]
    if words and len(words[0]) >= 6:       # a short first word ("data", "the") hits other companies' boards
        cands.append(words[0])
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def resolve_company(name: str, *, vendors: tuple[str, ...] = ("greenhouse", "lever", "ashby", "workable",
                                                                "recruitee", "teamtailor", "personio", "smartrecruiters"),
                    http: PoliteClient | None = None, require_eu: bool = False,
                    first_hit: bool = False) -> list[dict[str, Any]]:
    """Try slug candidates on public ATS boards; return boards that respond with ≥1 job.

    `require_eu`: only accept a board with an EU-eligible posting (we saw the company hiring in the EU, so a board
    without EU jobs is most likely another company with the same slug)."""
    own = http is None
    http = http or PoliteClient("ats_resolve", rps=1.0, max_requests=80)
    found = []
    try:
        for slug in slug_candidates(name):
            for vendor in vendors:
                try:
                    code, posts = FETCHERS[vendor](http, slug, {"company_name": name}, lambda t: False)
                except (Blocked, BudgetExceeded):
                    continue
                except Exception:  # noqa: BLE001
                    continue
                if code == 200 and posts:
                    if require_eu and not any(_eu_posting(p) for p in posts):
                        continue
                    found.append({"vendor": vendor, "slug": slug, "company_name": name, "jobs": len(posts)})
                    if first_hit:
                        break
            if found:
                break
    finally:
        if own:
            http.close()
    return found


def _eu_posting(p: Any) -> bool:
    return classify_location(workplace=p.workplace, title=p.title, location_raw=p.location_raw,
                             remote_scope=p.remote_scope, countries_structured=p.countries,
                             description="").section is not None


RESOLVE_VENDORS = ("personio", "teamtailor", "recruitee", "smartrecruiters", "workable", "greenhouse", "ashby", "lever")


def board_feedback_candidates(store: Any, *, days: int = 30, limit: int = 60) -> list[str]:
    """Companies with matched postings on job boards (not company ATS) that have no registry row yet.

    Registry rows with ats_type "none" (origin board_feedback_miss) are the negative cache: tried once, not found."""
    rows = store.query(
        """SELECT p.company AS company, p.company_norm AS company_norm, COUNT(*) AS n
           FROM v_matches_current m JOIN v_postings_current p USING (posting_key)
           WHERE m.decision = 'match' AND p.ats_type IS NULL AND p.source NOT LIKE 'ats_%'
             AND m.flags NOT LIKE '%"agency"%' AND p.company_norm IS NOT NULL AND p.company_norm != ''
             AND p.last_seen_at >= :since
           GROUP BY p.company_norm ORDER BY n DESC LIMIT 2000""", {"since": utcnow() - timedelta(days=days)})
    from jobradar.normalize.names import norm_company

    known = {norm_company(r.get("company_name") or "") for r in store.ats_registry()}
    known |= {r["ats_slug"] for r in store.ats_registry() if r["ats_type"] == "none"}
    return [r["company"] for r in rows if r["company_norm"] not in known and r["company"]][:limit]


def resolve_from_boards(store: Any, *, limit: int = 60, dry_run: bool = False) -> dict[str, int]:
    """Board → ATS feedback loop: find the company's own board, verified by at least one EU posting."""
    from jobradar.normalize.names import norm_company

    names = board_feedback_candidates(store, limit=limit)
    found_rows, misses = [], []
    now = utcnow()
    with PoliteClient("ats_resolve", rps=1.0, max_requests=limit * 18) as http:
        for name in names:
            try:
                found = resolve_company(name, vendors=RESOLVE_VENDORS, http=http, require_eu=True, first_hit=True)
            except BudgetExceeded:
                break
            for f in found:
                found_rows.append({"ats_type": f["vendor"], "ats_slug": f["slug"], "company_name": name,
                                   "extra": json.dumps({"company_name": name}, ensure_ascii=False),
                                   "origin": "board_feedback", "discovered_at": now})
            if not found:
                misses.append({"ats_type": "none", "ats_slug": norm_company(name), "company_name": name,
                               "extra": "{}", "origin": "board_feedback_miss", "discovered_at": now})
    if not dry_run:
        store.append("ats_registry", found_rows + misses)
    return {"companies": len(names), "found": len(found_rows), "not_found": len(misses)}


# ------------------------------------------------------------------ the nightly job
def run_ats(store: Any, *, dry_run: bool = False, minutes: float | None = None, resolve_limit: int = 40,
            max_boards: int | None = None) -> dict[str, Any]:
    """`jr ats`: registry upkeep, poll the boards due tonight within the time budget, board → ATS feedback loop."""
    from jobradar.pipeline.daily import budget_minutes

    full = config.module_on("ats")
    lite = not full and config.module_on("ats_lite_tier_a")
    if not full and not lite:
        return {"status": "off"}
    from jobradar.pipeline import minutes as governor

    started = time.monotonic()
    out: dict[str, Any] = {"status": "done"}
    if minutes is None:
        minutes, out["actions_minutes"] = governor.scale(store, "ats", budget_minutes("ats"))
        if out["actions_minutes"]["tight"] and full:
            full, lite = False, True                   # over the monthly projection: the smaller slice only
    out["lite"] = lite
    if not dry_run:
        out["registry"] = ensure_registry(store)
        extra = config.read_yaml(user_dir() / "companies.yaml")
        if extra.get("boards"):
            known = {(r["ats_type"], r["ats_slug"]) for r in store.ats_registry()}
            out["user_boards_added"] = add_user_boards(
                store, [b for b in extra["boards"] if (b.get("vendor"), b.get("slug")) not in known])
    plan = plan_boards(store, lite=lite, max_boards=max_boards or 1200, sweep_budget=LITE_TIER_A_MAX if lite else 1500)
    out["planned"] = len(plan)
    poll_deadline = started + minutes * (0.8 if full else 0.95) * 60
    out["poll"] = poll_boards(store, plan, dry_run=dry_run, deadline=poll_deadline)
    if full and not dry_run and time.monotonic() < started + minutes * 60 - 120:
        out["resolve"] = resolve_from_boards(store, limit=resolve_limit)
    out["minutes"] = round((time.monotonic() - started) / 60, 1)
    if not dry_run and get_settings().runner == "github":
        governor.record(store, "ats", out["minutes"])
    return out
