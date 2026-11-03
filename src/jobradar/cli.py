"""`jr` command line.

Setup and daily use:   jr setup · jr app · jr doctor
Overnight (Actions):   jr daily · jr ats · jr weekly
Sync:                  jr state pull · jr picks push|pull
Debugging:             jr sources · jr run · jr probe · jr explain · jr trace · jr sql · jr rematch · jr screen
Apply (Full):          jr prep · jr answer · jr cv · jr pack draft
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from jobradar.settings import get_settings

log = logging.getLogger("jobradar")


def make_store(path: str | None = None, *, read_only: bool = False):
    from jobradar.store.sqlite import SQLiteStore

    store = SQLiteStore(Path(path).expanduser() if path else get_settings().state_db, read_only=read_only)
    if not read_only:
        store.init()
    return store


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=1, default=str, ensure_ascii=False))


# ---------------------------------------------------------------- setup and status
def cmd_setup(a) -> int:
    from jobradar.app.server import serve

    path = "/setup" + (f"/{a.step}" if a.step else "") + ("?upgrade=1" if a.upgrade else "")
    serve(a.port, open_path=path, open_browser=not a.no_browser)
    return 0


def cmd_app(a) -> int:
    from jobradar.app.server import serve

    serve(a.port, open_path="/", open_browser=not a.no_browser, pull=not a.no_pull)
    return 0


def cmd_doctor(a) -> int:
    from jobradar.doctor import all_green, run_checks

    checks = run_checks(offline=a.offline)
    for c in checks:
        mark = "ok " if c.ok else ("-- " if c.optional else "!! ")
        fix = f"  (fix: jr setup --step {c.step})" if not c.ok and c.step else ""
        print(f"{mark}{c.label:24} {c.detail}{fix}")
    if a.smtp:
        from jobradar.digest import smtp

        try:
            smtp.verify_login()
            print("ok SMTP login")
        except Exception as exc:  # noqa: BLE001
            print(f"!! SMTP login: {exc}")
            return 1
    if a.estimate:
        from jobradar.llm.cost import estimate_night

        _print(estimate_night())
    return 0 if all_green(checks) else 1


def cmd_sources(a) -> int:
    from jobradar.sources.registry import load_specs

    if a.markdown:
        from jobradar.sources.catalog import markdown

        print(markdown(), end="")
        return 0
    s = get_settings()
    mine = load_specs()
    for sid, spec in load_specs(all_sources=a.all).items():
        keys = "" if not spec.secrets else ("keys ok" if s.has_secrets(spec.secrets) else "NEEDS " + ",".join(spec.secrets))
        on = "on " if sid in mine else "off"
        print(f"{on} {sid:28} {','.join(spec.countries):12} every {spec.every_days}d runner={spec.runner:6} {keys}")
    return 0


# ---------------------------------------------------------------- overnight
def cmd_daily(a) -> int:
    from jobradar.pipeline.daily import run_daily

    store = make_store(a.db)
    res = run_daily(store, picks=Path(a.picks) if a.picks else None, dry_run=a.dry_run, force=a.force,
                    only=a.sources or None, skip_fetch=a.skip_fetch, minutes=a.minutes)
    if not a.verbose:
        res.get("shortlist", {}).pop("preview", None)
    _print(res)
    store.close()
    return 0


def cmd_ats(a) -> int:
    from jobradar.pipeline.ats import run_ats

    store = make_store(a.db)
    _print(run_ats(store, dry_run=a.dry_run, minutes=a.minutes, max_boards=a.max_boards))
    store.close()
    return 0


def cmd_weekly(a) -> int:
    from jobradar.pipeline.weekly import run_weekly, weekly_text

    store = make_store(a.db)
    if a.picks:
        from jobradar.pipeline.daily import read_picks

        store.import_decisions(read_picks(Path(a.picks)))
    stats = run_weekly(store, dry_run=a.dry_run, email=a.email or None)
    print(weekly_text(stats))
    store.close()
    return 0


def cmd_rematch(a) -> int:
    from jobradar.pipeline.daily import ensure_rule_version

    print(f"re-matched {ensure_rule_version(make_store(a.db), days=a.days, force=a.force)} postings")
    return 0


# ---------------------------------------------------------------- sync
def cmd_state(a) -> int:
    from jobradar.local.sync import pull_state

    _print(pull_state())
    return 0


def cmd_picks(a) -> int:
    from jobradar.local.db import open_local
    from jobradar.local.sync import pull_picks, push_picks

    db = open_local()
    _print(push_picks(db) if a.action == "push" else pull_picks(db))
    return 0


# ---------------------------------------------------------------- debugging
def cmd_run(a) -> int:
    from jobradar.pipeline.run import plan_sources, run_source

    store = None if a.dry_run else make_store(a.db)
    runner = a.runner or get_settings().runner
    sources = a.sources or plan_sources(store or make_store(a.db), runner=runner)
    if not sources:
        print(f"No sources due for runner '{runner}' (each runs every_days after its last success). "
              "Name sources to run them anyway, e.g. jr run arbeitnow", flush=True)
    for sid in sources:
        summary = run_source(sid, store, dry_run=a.dry_run, max_requests=a.max_requests, trigger="cli")
        print(f"{sid:24} {summary.status:14} fetched={summary.fetched:5} new={summary.new_versions:5} "
              f"matched={summary.matched:4} req={summary.requests:3} {summary.error or ''}", flush=True)
        if a.verbose:
            _print(summary.to_dict())
    return 0


def cmd_probe(a) -> int:
    from jobradar.pipeline.run import probe_row, run_source

    record = Path(a.record_dir or "tests/fixtures") / a.source if a.record else None
    summary = run_source(a.source, None, dry_run=True, max_requests=a.max_requests, record_dir=record)
    _print(summary.to_dict())
    if a.save:
        make_store(a.db).append("source_probes", [probe_row(summary, get_settings().runner)])
    return 0 if summary.fetched else 2


def cmd_explain(a) -> int:
    store = make_store(a.db, read_only=a.read_only)
    keys = {a.key}
    for r in store.query("SELECT member_posting_keys FROM shortlist WHERE job_id = :k", {"k": a.key}, table="shortlist"):
        keys |= set(r["member_posting_keys"] or [])
    marks = ",".join("?" for _ in keys)
    rows = store.query(f"SELECT * FROM matches WHERE posting_key IN ({marks}) ORDER BY matched_at DESC LIMIT 5",
                       list(keys), table="matches")
    for r in rows:
        r["explain"] = json.loads(r["explain"]) if isinstance(r.get("explain"), str) else r.get("explain")
    if not rows:
        from jobradar.match.engine import MatchEngine

        print(f"{a.key}: not in state.db; title check under today's rules:")
        _print(MatchEngine.load().title_verdict(a.key).__dict__)
        return 1
    _print(rows)
    return 0


def cmd_trace(a) -> int:
    store = make_store(a.db, read_only=a.read_only)
    _print(store.query("SELECT * FROM v_job_journey WHERE LOWER(company) LIKE :c ORDER BY shortlisted_on",
                       {"c": f"%{a.company.lower()}%"}))
    return 0


def cmd_sql(a) -> int:
    q = a.query.strip()
    if not q.lower().startswith(("select", "with")):
        print("read-only: only SELECT/WITH queries")
        return 1
    _print(make_store(a.db, read_only=True).query(q))
    return 0


def find_job(store: Any, job_id: str) -> dict[str, Any] | None:
    """The newest shortlist row for a job id, or a hand-added job from local.db."""
    rows = store.query("SELECT * FROM shortlist WHERE job_id = :j ORDER BY night DESC LIMIT 1", {"j": job_id},
                       table="shortlist")
    return rows[0] if rows else None


def cmd_screen(a) -> int:
    from jobradar.apply.screen import screen_jobs

    row = find_job(make_store(a.db, read_only=True), a.job)
    if not row:
        print(f"{a.job}: not in the shortlist")
        return 1
    _print(screen_jobs([{"job_id": row["job_id"], "title": row["title"], "company": row["company"],
                         "location": row["location_display"], "description": row.get("description") or ""}]))
    return 0


# ---------------------------------------------------------------- apply (Full)
def cmd_prep(a) -> int:
    from jobradar.apply.prep import (PrepHttp, apply_prep_writes, job_from_row, load_cached, pasted_from, prep_job,
                                      reserve_prep)
    from jobradar.llm.client import LLMClient, optional_client, step_provider
    from jobradar.local.db import open_local

    db = open_local()
    db.seed_bank()
    bank = db.bank()
    if a.url:
        jobs = [{"job_id": a.job or "M-preview", "title": a.title or "", "company": a.company or "",
                 "apply_url": a.url, "posting_url": a.url, "family": None, "instructions": []}]
    else:
        store = make_store(read_only=True)
        ids = [a.job] if a.job else [jid for jid, e in db.statuses().items() if e["status"] == "picked"]
        jobs = []
        for jid in ids:
            row = find_job(store, jid) or {}
            local = db.job(jid)
            if not row and not local:
                print(f"{jid}: not in the shortlist or local.db")
                continue
            jobs.append(job_from_row(row, local))
    llm = LLMClient("prep") if step_provider("prep") not in ("off", "") else None
    drafter = optional_client("draft")
    with PrepHttp() as http:
        for job in jobs:
            reserve_prep()
            try:
                prep, match = prep_job(job, bank, http, llm, posting_text=job.get("description") or "",
                                       draft_client=drafter, pasted=pasted_from(load_cached(job["job_id"])))
            except Exception as exc:  # noqa: BLE001
                print(f"{job['job_id']:10} failed: {type(exc).__name__}: {exc}"[:200])
                continue
            if not a.dry_run:
                apply_prep_writes(db, bank, prep, match)
            print(f"{prep['job_id']:10} {prep['route']['vendor']:12} {prep['prep']:14} effort={prep['effort_min']:3} "
                  f"{json.dumps(prep['counts'])} llm_calls={match.llm_calls if match else 0}", flush=True)
            if a.dry_run or a.verbose:
                for it in prep["items"]:
                    print(f"    {it['status']:8} {'*' if it['required'] else ' '} {it['type']:11} {it['label'][:70]}"
                          f"{' → ' + it['answer_id'] if it['answer_id'] else ''}")
    if llm:
        llm.close()
    return 0


def cmd_answer(a) -> int:
    """Set one answer of a prepped job (from an editing session): text from --text or stdin."""
    from jobradar.apply.bank import pick_option
    from jobradar.apply.prep import approve_answer, load_cached, recount, save_cached
    from jobradar.apply.route import EFFORT_MIN
    from jobradar.local.db import open_local

    p = load_cached(a.job)
    if not p:
        print(f"{a.job}: no prep saved; prep it in the app first")
        return 1
    it = next((x for x in p["items"] if x["key"] == a.key), None)
    if not it:
        print(f"{a.job}: no question with key {a.key!r}; keys: {', '.join(x['key'] for x in p['items'])}")
        return 1
    text = (a.text if a.text is not None else sys.stdin.read()).strip()
    if not text:
        print("empty answer; nothing saved")
        return 1
    if a.bank:
        db = open_local()
        approve_answer(db, db.bank(), it, text, company=p.get("company", ""))
    it.update(answer=text, status="once", drafted=False, edited="editor")
    if it["type"] in ("select", "multiselect", "boolean"):
        it["option"] = pick_option(text, it["options"], it["type"]) or ""
    recount(p, EFFORT_MIN.get(p["route"]["vendor"], 6))
    save_cached(p)
    print(f"{a.job} {a.key}: saved for this application" + (" and to the bank (Approved)" if a.bank else ""))
    return 0


def cmd_cv(a) -> int:
    from jobradar.apply.cv import build, default_source

    try:
        done = build(Path(a.source).expanduser() if a.source else default_source(), a.variant or None)
    except ImportError:
        print("CV PDFs need Playwright: pip install 'jobradar-eu[full]' && playwright install chromium")
        return 1
    for name, path in done.items():
        print(f"{name:8} {path}")
    return 0


def cmd_pack(a) -> int:
    from jobradar.match.packdraft import draft_pack, save_pack

    res = draft_pack(a.description, name=a.name)
    _print({k: v for k, v in res.items() if k != "pack"})
    if a.save and res["ok"]:
        print(f"saved to {save_pack(res['pack'], a.name)}")
    return 0 if res["ok"] else 1


def cmd_llm(a) -> int:
    from jobradar.llm.client import LLMError, test_call

    try:
        _print(test_call(a.step))
    except LLMError as exc:
        print(f"LLM test failed: {exc}")
        return 1
    return 0


# ---------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jr", description="Job Radar: an overnight job shortlist and a local app to "
                                                       "pick and apply. Start with `jr setup`.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def db_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--db", help="state.db path (default: $JR_STATE_DB or ~/.job-radar/state.db)")

    sp = sub.add_parser("setup", help="open the setup wizard in the browser")
    sp.add_argument("--step", help="reopen one step (e.g. llm, roles, config_repo)")
    sp.add_argument("--upgrade", action="store_true", help="switch Lite to Full and run the Full-only steps")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-browser", action="store_true")
    sp.set_defaults(fn=cmd_setup)
    sp = sub.add_parser("app", help="open the morning pick-and-apply view")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-browser", action="store_true")
    sp.add_argument("--no-pull", action="store_true", help="don't download the latest state.db first")
    sp.set_defaults(fn=cmd_app)
    sp = sub.add_parser("doctor", help="check setup, keys, repo, last run and Actions minutes")
    sp.add_argument("--smtp", action="store_true", help="also test the SMTP login")
    sp.add_argument("--offline", action="store_true", help="skip the GitHub checks")
    sp.add_argument("--estimate", action="store_true", help="show the LLM cost estimate per night")
    sp.set_defaults(fn=cmd_doctor)
    sp = sub.add_parser("sources", help="list sources (on = runs for you)")
    sp.add_argument("--all", action="store_true", help="also sources not selected for you")
    sp.add_argument("--markdown", action="store_true", help="print the shipped source list as Markdown (docs/sources.md)")
    sp.set_defaults(fn=cmd_sources)

    sp = sub.add_parser("daily", help="overnight: picks → fetch → shortlist (what the workflow runs)")
    db_arg(sp)
    sp.add_argument("--picks", help="picks.jsonl from the picks branch")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--force", action="store_true", help="build a shortlist even if tonight's exists")
    sp.add_argument("--skip-fetch", action="store_true", help="only rebuild the shortlist")
    sp.add_argument("--minutes", type=float, help="run time budget (default from user/settings.yaml)")
    sp.add_argument("--verbose", action="store_true")
    sp.add_argument("sources", nargs="*", help="only these sources")
    sp.set_defaults(fn=cmd_daily)
    sp = sub.add_parser("ats", help="overnight: poll company ATS boards (Full)")
    db_arg(sp)
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--minutes", type=float)
    sp.add_argument("--max-boards", type=int)
    sp.set_defaults(fn=cmd_ats)
    sp = sub.add_parser("weekly", help="weekly stats (+ VACUUM)")
    db_arg(sp)
    sp.add_argument("--picks")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--email", action="store_true")
    sp.set_defaults(fn=cmd_weekly)
    sp = sub.add_parser("rematch", help="re-apply the matching rules to recent postings")
    db_arg(sp)
    sp.add_argument("--days", type=int, default=30)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_rematch)

    sp = sub.add_parser("state", help="state pull: download the newest state.db from the config repo")
    sp.add_argument("action", choices=["pull"])
    sp.set_defaults(fn=cmd_state)
    sp = sub.add_parser("picks", help="picks push|pull: sync your decisions with the config repo")
    sp.add_argument("action", choices=["push", "pull"])
    sp.set_defaults(fn=cmd_picks)

    sp = sub.add_parser("run", help="run sources (default: those due now)")
    db_arg(sp)
    sp.add_argument("sources", nargs="*")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--runner", choices=["github", "local"])
    sp.add_argument("--max-requests", type=int)
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(fn=cmd_run)
    sp = sub.add_parser("probe", help="test one source (no writes); --record saves fixtures")
    db_arg(sp)
    sp.add_argument("source")
    sp.add_argument("--record", action="store_true")
    sp.add_argument("--record-dir", help="fixture root (default tests/fixtures)")
    sp.add_argument("--save", action="store_true", help="store the probe result in state.db")
    sp.add_argument("--max-requests", type=int)
    sp.set_defaults(fn=cmd_probe)
    sp = sub.add_parser("explain", help="why a posting key / JR- job id matched or dropped")
    db_arg(sp)
    sp.add_argument("key")
    sp.add_argument("--read-only", action="store_true")
    sp.set_defaults(fn=cmd_explain)
    sp = sub.add_parser("trace", help="every shortlisted job of a company and what you did with it")
    db_arg(sp)
    sp.add_argument("company")
    sp.add_argument("--read-only", action="store_true")
    sp.set_defaults(fn=cmd_trace)
    sp = sub.add_parser("sql", help="read-only SQL on state.db")
    db_arg(sp)
    sp.add_argument("query")
    sp.set_defaults(fn=cmd_sql)
    sp = sub.add_parser("screen", help="debug: LLM screen of one shortlisted job")
    db_arg(sp)
    sp.add_argument("job")
    sp.set_defaults(fn=cmd_screen)

    sp = sub.add_parser("prep", help="read the application forms of picked jobs and match the answer bank")
    sp.add_argument("--job")
    sp.add_argument("--url", help="prep a form by URL")
    sp.add_argument("--title")
    sp.add_argument("--company")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(fn=cmd_prep)
    sp = sub.add_parser("answer", help="set one answer of a prepped job (text from --text or stdin)")
    sp.add_argument("job")
    sp.add_argument("--key", required=True)
    sp.add_argument("--text")
    sp.add_argument("--bank", action="store_true", help="also save it to the bank as Approved")
    sp.set_defaults(fn=cmd_answer)
    sp = sub.add_parser("cv", help="render CV variants (PDF) from ~/.job-radar/cv.yaml")
    sp.add_argument("--source")
    sp.add_argument("--variant", action="append")
    sp.set_defaults(fn=cmd_cv)
    sp = sub.add_parser("pack", help="pack draft \"<description>\": an LLM drafts a custom role pack")
    sp.add_argument("action", choices=["draft"])
    sp.add_argument("description")
    sp.add_argument("--name", default="custom")
    sp.add_argument("--save", action="store_true", help="save to user/packs/<name>.yaml")
    sp.set_defaults(fn=cmd_pack)
    sp = sub.add_parser("llm", help="llm test [--step screen]: one tiny call to check the key and model")
    sp.add_argument("action", choices=["test"])
    sp.add_argument("--step", default="screen")
    sp.set_defaults(fn=cmd_llm)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("JR_LOG_LEVEL", "WARNING"), format="%(levelname)s %(name)s: %(message)s")
    a = build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
