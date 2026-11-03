"""The local app (`jr app` / `jr setup` → http://127.0.0.1:8765): the morning pick-and-apply view, the job pages,
the answer bank, weekly stats and the setup wizard (/setup).

Data: the overnight results come from state.db (pulled from the config repo's `state` branch, opened read-only);
everything the user does lives in ~/.job-radar/local.db and decisions are pushed to the `picks` branch in small
batches (after a few seconds of quiet, on "Sync" and when the page closes).

Full mode adds prep (read the form, match the answer bank, per-job drafts), CV/cover-letter PDFs and auto-fill through
the Chrome extension (``extension/``; it never submits). Slow actions run as background tasks polled at /task/<key>.

Local only (binds 127.0.0.1). Every POST must carry the ``X-JR: 1`` header, so a web page in another tab can't drive it.
"""

from __future__ import annotations

import json
import logging
import platform
import shutil
import subprocess
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for

from jobradar import config
from jobradar.apply import claude_edit, cover_letter, fill
from jobradar.apply import prep as prep_mod
from jobradar.apply.bank import pick_option, render
from jobradar.apply.posting import read_posting
from jobradar.apply.prep import recount
from jobradar.apply.profile import cv_options
from jobradar.apply.route import EFFORT_MIN, Route, route_for_url
from jobradar.local.db import STATUSES, LocalDB, open_local
from jobradar.settings import get_settings

log = logging.getLogger(__name__)

STATUS_BADGE = {"missing": ("Needs your answer", "warn"), "choose": ("Pick an option", "warn"),
                "adjust": ("Check and adjust", "accent"), "bank": ("From the bank", ""),
                "once": ("Set for this application", "good"), "auto": ("Fact", ""), "file": ("Upload your CV", ""),
                "skip": ("Optional, nothing stored", "muted")}
PREP_LABEL = {"manual form": "add questions"}
TABS = [("new", "New"), ("picked", "Picked"), ("later", "Later"), ("applied", "Applied"), ("skipped", "Skipped")]
TAB_STATUSES = {"new": ("new",), "picked": ("picked",), "later": ("later",),
                "applied": ("applied", "screening", "interview", "offer", "rejected"), "skipped": ("skipped",)}
STAGES = ("applied", "screening", "interview", "offer", "rejected")
SECTION_ORDER = {"top": 0, "remote": 1, "home": 2, "elsewhere": 3, "review": 4}
SECTION_LABEL = {"top": "Top pick", "remote": "Remote", "home": "Near home", "elsewhere": "Elsewhere", "review": "Review"}
ALL = "__all__"
PUSH_AFTER_SECONDS = 8
SHORTLIST_DAYS = 60


class Task:
    """A slow action running on a background thread. ``step`` records what it is doing now (shown live in the app)."""

    def __init__(self, key: str, what: str):
        self.key, self.what, self.started = key, what, time.time()
        self.steps: list[dict[str, Any]] = []
        self.now = ""
        self.done_n = self.total = 0
        self.running, self.error = True, ""
        self.result: dict[str, Any] = {}
        self.thread: threading.Thread | None = None

    def step(self, msg: str, kind: str = "") -> None:
        self.steps.append({"msg": msg, "t": round(time.time() - self.started), "kind": kind})
        if not kind:
            self.now = msg

    def to_dict(self) -> dict[str, Any]:
        return {"what": self.what, "running": self.running, "elapsed": round(time.time() - self.started, 1),
                "steps": self.steps, "now": self.now, "done": self.done_n, "total": self.total,
                "error": self.error, "result": self.result}


class AppState:
    def __init__(self, db: LocalDB, *, state_path: Path | None = None, llm_factory: Callable[[], Any] | None = None,
                 http_factory: Callable[[], Any] | None = None, syncer: Any = None):
        from jobradar.llm.client import optional_client, step_provider

        self.db = db
        self.state_path = state_path or get_settings().state_db
        self.llm_factory = llm_factory or (lambda: _llm_or_none("prep"))
        self.http_factory = http_factory or prep_mod.PrepHttp
        self.syncer = syncer
        try:
            self.drafter = optional_client("draft") if step_provider("draft") not in ("off", "") else None
        except Exception:  # noqa: BLE001 - no draft model is fine
            self.drafter = None
        self.lock = threading.RLock()
        self.tasks: dict[str, Task] = {}
        self.task_lock = threading.Lock()
        self.fill_reports: dict[str, list[dict[str, Any]]] = {}
        self.push_timer: threading.Timer | None = None
        self.sync_status: dict[str, Any] = {}
        self.runs_cache: tuple[float, Any] = (0.0, None)
        db.seed_bank()
        self.reload()

    # ------------------------------------------------------------------ data
    def store(self):
        from jobradar.store.sqlite import SQLiteStore

        if not self.state_path.is_file():
            return None
        return SQLiteStore(self.state_path, read_only=True)

    def reload(self) -> None:
        """Jobs = the newest shortlist row of each job of the last 60 days + hand-added jobs from local.db."""
        jobs: dict[str, dict[str, Any]] = {}
        night = None
        store = self.store()
        if store is not None:
            try:
                rows = store.query(
                    """SELECT * FROM shortlist WHERE night >= date('now', :days) ORDER BY night""",
                    {"days": f"-{SHORTLIST_DAYS} days"}, table="shortlist")
                for r in rows:
                    jobs[r["job_id"]] = r
                last = store.query("SELECT * FROM nights ORDER BY finished_at DESC LIMIT 1", table="nights")
                night = last[0] if last else None
            finally:
                store.close()
        for m in self.db.manual_jobs():
            jobs[m["job_id"]] = {"job_id": m["job_id"], "title": m["title"], "company": m["company"],
                                 "location_display": m.get("location") or "", "url": m.get("apply_url") or m["url"],
                                 "posting_url": m["url"], "description": m.get("description") or "",
                                 "section": "review", "family": m.get("family"), "manual": True,
                                 "night": (m.get("created_at") or "")[:10], "sources": ["added by hand"]}
        for jid, j in jobs.items():
            for k in ("why", "copies", "routes"):
                if isinstance(j.get(k), str):
                    try:
                        j[k] = json.loads(j[k])
                    except ValueError:
                        j[k] = None
        with self.lock:
            self.jobs = jobs
            self.last_night = night

    def statuses(self) -> dict[str, dict[str, Any]]:
        return self.db.statuses()

    def job(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if not job:
            self.reload()
            job = self.jobs.get(job_id)
        if not job:
            abort(404)
        return job

    def prep_job_dict(self, job_id: str) -> dict[str, Any]:
        return prep_mod.job_from_row(self.job(job_id), self.db.job(job_id))

    def prep(self, job_id: str) -> dict[str, Any] | None:
        return self.db.prep(job_id)

    # ------------------------------------------------------------------ decisions + sync
    def decide(self, job_id: str, status: str, note: str | None = None) -> dict[str, Any]:
        job = self.job(job_id)
        row = self.db.record(job_id, status, title=job.get("title") or "", company=job.get("company") or "",
                             family=job.get("family"), source=(job.get("sources") or [None])[0],
                             route=job.get("route_vendor"), dedup_keys=job.get("dedup_keys") or [], note=note)
        self.schedule_push()
        return row

    def schedule_push(self, delay: float = PUSH_AFTER_SECONDS) -> None:
        if self.syncer is None:
            return
        if self.push_timer is not None:
            self.push_timer.cancel()
        self.push_timer = threading.Timer(delay, self.push_now)
        self.push_timer.daemon = True
        self.push_timer.start()

    def push_now(self) -> dict[str, Any]:
        if self.syncer is None:
            return {"pushed": 0, "off": True}
        try:
            res = self.syncer.push_picks(self.db)
            self.sync_status = {"ok": True, "at": _now(), **res}
        except Exception as exc:  # noqa: BLE001 - shown in the status bar; retried on the next decision
            self.sync_status = {"ok": False, "at": _now(), "error": str(exc)[:200]}
            res = {"pushed": 0, "error": str(exc)[:200]}
        return res

    def pull(self) -> dict[str, Any]:
        if self.syncer is None:
            return {"off": True}
        out: dict[str, Any] = {}
        try:
            out["state"] = self.syncer.pull_state(self.state_path)
            out["picks"] = self.syncer.pull_picks(self.db)
            self.sync_status = {"ok": True, "at": _now(), "pulled": True}
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)[:200]
            self.sync_status = {"ok": False, "at": _now(), "error": out["error"]}
        self.reload()
        return out

    def runs(self) -> Any:
        """Latest workflow runs, cached five minutes."""
        at, cached = self.runs_cache
        if self.syncer is None or time.time() - at < 300:
            return cached
        try:
            runs = self.syncer.workflow_runs(limit=3)
        except Exception:  # noqa: BLE001
            runs = None
        self.runs_cache = (time.time(), runs)
        return runs

    # ------------------------------------------------------------------ tasks and prep
    def start(self, key: str, what: str, fn: Callable[[Task], dict[str, Any]]) -> tuple[Task, bool]:
        with self.task_lock:
            old = self.tasks.get(key)
            if old and old.running:
                return old, False
            task = self.tasks[key] = Task(key, what)

        def run() -> None:
            try:
                task.result = fn(task) or {}
            except Exception as exc:  # noqa: BLE001 - shown in the app
                log.warning("%s failed: %s", what, type(exc).__name__)
                task.error = f"{type(exc).__name__}: {exc}"[:300]
            finally:
                task.running = False

        task.thread = threading.Thread(target=run, daemon=True)
        task.thread.start()
        return task, True

    def run_prep(self, job: dict[str, Any], http: Any, llm: Any, pasted: list[Any] | None = None,
                 step: prep_mod.Step = prep_mod._quiet) -> dict[str, Any]:
        """Prep one job. ``pasted`` replaces the questions pasted earlier ([] clears them); None keeps them."""
        prep_mod.reserve_prep()
        old = self.prep(job["job_id"])
        if not self.lock.acquire(blocking=False):
            step("Waiting for the prep that is already running")
            self.lock.acquire()
        try:
            bank = self.db.bank()
            p, match = prep_mod.prep_job(job, bank, http, llm, posting_text=job.get("description") or "",
                                         draft_client=self.drafter,
                                         pasted=prep_mod.pasted_from(old) if pasted is None else pasted, step=step)
        finally:
            self.lock.release()
        keep_own_answers(p, old)
        keep_cv_choice(p, old)
        recount(p, EFFORT_MIN.get(p["route"]["vendor"], 6))
        step("Saving")
        prep_mod.apply_prep_writes(self.db, bank, p, match)
        return p

    def prep_all(self, task: Task, job_ids: list[str]) -> dict[str, Any]:
        task.total = len(job_ids)
        out = {"ready": 0, "manual": 0, "failed": 0}
        with self.http_factory() as http:
            llm = self.llm_factory()
            try:
                for jid in job_ids:
                    job = self.prep_job_dict(jid)
                    name = job.get("company") or job.get("title") or jid
                    try:
                        p = self.run_prep(job, http, llm, step=lambda m, n=name: setattr(task, "now", f"{n}: {m}"))
                        if p["prep"] == "manual form":
                            out["manual"] += 1
                            task.step(f"{name}: can't read the form ({p['route']['vendor']}), add its questions", "warn")
                        else:
                            out["ready"] += 1
                            task.step(f"{name}: {len(p['items'])} questions read and answered", "ok")
                    except Exception as exc:  # noqa: BLE001
                        out["failed"] += 1
                        task.step(f"{name}: prep failed ({type(exc).__name__})", "fail")
                    task.done_n += 1
            finally:
                if llm is not None:
                    llm.close()
        return out


def _llm_or_none(step: str) -> Any:
    from jobradar.llm.client import LLMClient, step_provider

    return LLMClient(step) if step_provider(step) not in ("off", "") else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ item helpers
def keep_own_answers(new: dict[str, Any], old: dict[str, Any] | None) -> int:
    """Re-prep keeps what the user set for this application ("once") when the same question is still on the form."""
    mine = {it["key"]: it for it in (old or {}).get("items") or [] if it.get("status") == "once"}
    kept = 0
    for it in new.get("items") or []:
        o = mine.get(it["key"])
        if o and o.get("label") == it["label"]:
            it.update(answer=o.get("answer", ""), option=o.get("option", ""), status="once", drafted=False)
            kept += 1
    return kept


def keep_cv_choice(new: dict[str, Any], old: dict[str, Any] | None) -> None:
    """Re-prep keeps a CV the user picked by hand for this job."""
    if old and old.get("cv_why") == "picked by hand":
        new.update(cv=old["cv"], cv_variant=old["cv_variant"], cv_why=old["cv_why"])


def set_item_answer(it: dict[str, Any], text: str, status: str) -> None:
    it["answer"] = text
    it["status"] = status
    if it["type"] in ("select", "multiselect", "boolean"):
        it["option"] = pick_option(text, it["options"], it["type"]) or (text if text in (it["options"] or []) else "")
        if not it["option"]:
            it["status"] = "choose"


def sent_value(it: dict[str, Any]) -> str:
    return it.get("option") or it.get("answer") or ""


def open_in_chrome(url: str) -> bool:
    """Open an app page in Google Chrome, where the Job Radar extension is installed."""
    system = platform.system()
    if system == "Darwin":
        cmd = ["open", "-a", "Google Chrome", url]
    elif system == "Windows":
        cmd = ["cmd", "/c", "start", "chrome", url]
    else:
        exe = next((shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
                    if shutil.which(n)), None)
        if not exe:
            return False
        cmd = [exe, url]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def morning_rows(state: AppState, tab: str, flt: dict[str, str]) -> list[dict[str, Any]]:
    statuses = state.statuses()
    want = TAB_STATUSES.get(tab, ("new",))
    show_hidden = flt.get("hidden") == "1"
    rows = []
    for jid, j in state.jobs.items():
        st = (statuses.get(jid) or {}).get("status") or "new"
        if st not in want:
            continue
        if tab == "new" and j.get("hidden") and not show_hidden:
            continue
        if flt.get("family") and j.get("family") != flt["family"]:
            continue
        if flt.get("setup") and j.get("section") != flt["setup"]:
            continue
        if flt.get("country") and flt["country"] not in (j.get("countries") or []):
            continue
        if flt.get("fit") and (j.get("fit") or 0) < int(flt["fit"]):
            continue
        rows.append({**j, "status": st, "status_at": (statuses.get(jid) or {}).get("at")})
    if tab == "new":
        rows.sort(key=lambda r: (SECTION_ORDER.get(r.get("section") or "", 9), -(r.get("fit") or 0),
                                 -(r.get("priority") or 0), r.get("job_id")))
    else:
        rows.sort(key=lambda r: str(r.get("status_at") or ""), reverse=True)
    return rows


# ------------------------------------------------------------------------- app
FLAG_TEXT = {"worldwide": "remote, worldwide", "scope?": "remote, region not stated",
             "mode?": "work setup not stated", "location?": "location not stated",
             "remote option?": "may allow remote work", "relocation": "needs a move", "agency": "recruitment agency"}


def flag_text(flag: str) -> str:
    """A matcher flag in plain words for the job list ("residency:DE" → "only for residents of Germany")."""
    if flag.startswith("residency:"):
        from jobradar.wizard import country_names

        code = flag.split(":", 1)[1]
        return f"only for residents of {country_names().get(code, code)}"
    return FLAG_TEXT.get(flag, flag)


def create_app(db: LocalDB | None = None, *, state_path: Path | None = None, llm_factory: Callable[[], Any] | None = None,
               http_factory: Callable[[], Any] | None = None, syncer: Any = "auto", pull: bool = False) -> Flask:
    if syncer == "auto":
        from jobradar.local import sync as syncer_mod

        try:
            syncer_mod.config_repo()
            syncer = syncer_mod
        except syncer_mod.SyncError:
            syncer = None
    app = Flask(__name__)
    app.jinja_env.tests["startswith"] = lambda s, prefix: str(s).startswith(prefix)
    app.jinja_env.filters["flag_text"] = flag_text
    app.jinja_env.globals.update(prep_label=lambda v: PREP_LABEL.get(v, v), badge=STATUS_BADGE, tabs=TABS,
                                 section_label=SECTION_LABEL, stages=STAGES, full=lambda: config.mode() == "full",
                                 module_on=config.module_on)
    state = AppState(db or open_local(), state_path=state_path, llm_factory=llm_factory, http_factory=http_factory,
                     syncer=syncer)
    if pull and syncer is not None:
        state.pull()
    app.config["state"] = state

    from jobradar.app.setup import bp as setup_bp

    app.register_blueprint(setup_bp)

    @app.before_request
    def guard() -> None:
        host = (request.host or "").rsplit(":", 1)[0]
        if host not in ("localhost", "127.0.0.1", "[::1]"):      # DNS-rebinding guard: local only
            abort(403)
        if request.method == "POST" and request.headers.get("X-JR") != "1":
            abort(403)

    def save(p: dict[str, Any]) -> None:
        recount(p, EFFORT_MIN.get(p["route"]["vendor"], 6))
        state.db.save_prep(p)

    def started(task: Task, new: bool, **extra: Any) -> Any:
        return jsonify(ok=True, task=task.key, already=not new, **extra)

    def prep_task(job: dict[str, Any], what: str, pasted_text: str | None = None, pasted: list[Any] | None = None) -> Any:
        def run(task: Task) -> dict[str, Any]:
            with state.http_factory() as http:
                llm = state.llm_factory()
                try:
                    questions = pasted
                    if pasted_text is not None:
                        questions = []
                        if pasted_text:
                            task.step("Splitting the pasted text into questions")
                            questions = prep_mod.parse_pasted(pasted_text, llm)
                            if not questions:
                                raise ValueError("no questions found in the pasted text")
                    p = state.run_prep(job, http, llm, pasted=questions, step=task.step)
                finally:
                    if llm is not None:
                        llm.close()
            return {"prep": p["prep"], "vendor": p["route"]["vendor"], "questions": len(p["items"])}

        return started(*state.start(job["job_id"], what, run))

    @app.context_processor
    def status_bar() -> dict[str, Any]:
        return {"last_night": state.last_night, "sync": state.sync_status, "has_sync": state.syncer is not None,
                "mode": config.mode()}

    # ---------------------------------------------------------------- morning view
    @app.get("/")
    def morning() -> Any:
        if not (config.profile() or {}).get("location") and not state.jobs:
            return redirect(url_for("setup.index"))
        tab = request.args.get("tab", "new")
        flt = {k: request.args.get(k, "") for k in ("family", "setup", "country", "fit", "hidden")}
        rows = morning_rows(state, tab, flt)
        statuses = state.statuses()
        counts = {t: sum(1 for jid, j in state.jobs.items()
                         if ((statuses.get(jid) or {}).get("status") or "new") in TAB_STATUSES[t]
                         and not (t == "new" and j.get("hidden"))) for t, _ in TABS}
        families = sorted({j.get("family") for j in state.jobs.values() if j.get("family")})
        countries = sorted({c for j in state.jobs.values() for c in (j.get("countries") or [])})
        hidden = sum(1 for jid, j in state.jobs.items() if j.get("hidden") and
                     ((statuses.get(jid) or {}).get("status") or "new") == "new")
        return render_template("morning.html", rows=rows, tab=tab, flt=flt, counts=counts, families=families,
                               countries=countries, hidden=hidden, runs=state.runs())

    @app.post("/decide")
    def decide() -> Any:
        data = request.get_json(force=True) or {}
        status = data.get("status")
        if status not in STATUSES:
            return jsonify(ok=False, error=f"unknown status {status!r}"), 400
        row = state.decide(str(data.get("job_id")), status, note=data.get("note"))
        return jsonify(ok=True, status=row["status"], at=row["at"])

    @app.post("/sync")
    def sync() -> Any:
        out = {"push": state.push_now(), "pull": state.pull()}
        return jsonify(ok="error" not in out["pull"], **out)

    @app.post("/close")
    def close() -> Any:
        """The page is closing (sendBeacon): push what is left."""
        state.push_now()
        return jsonify(ok=True)

    @app.post("/reload")
    def reload() -> Any:
        state.reload()
        return jsonify(ok=True, jobs=len(state.jobs))

    @app.get("/status")
    def status() -> Any:
        from jobradar.local import sync as s

        minutes = s.minutes_used() if state.syncer is not None else None
        return jsonify(last_night=state.last_night, sync=state.sync_status, runs=state.runs(), minutes=minutes)

    @app.post("/ext/seen")
    def ext_seen() -> Any:
        state.db.set_meta("extension_seen", _now())
        return jsonify(ok=True)

    # ---------------------------------------------------------------- job page + apply flow
    @app.get("/task/<key>")
    def task_status(key: str) -> Any:
        task = state.tasks.get(key)
        return jsonify(task.to_dict() if task else {"running": False, "missing": True})

    @app.get("/job/<job_id>")
    def job_page(job_id: str) -> str:
        job = state.job(job_id)
        p = state.prep(job_id)
        items = (p or {}).get("items") or []
        summary = {"needs": sum(it["status"] in ("missing", "choose") for it in items),
                   "check": sum(it["status"] == "adjust" for it in items),
                   "ready": sum(it["status"] in ("bank", "once", "auto", "file") for it in items)}
        task = state.tasks.get(job_id)
        guess = None if p else prep_mod.route_for_job(state.prep_job_dict(job_id), None)
        return render_template("job.html", job=job, p=p, guess=guess, items=items, summary=summary,
                               status=state.db.status(job_id), history=state.db.history(job_id),
                               local=state.db.job(job_id), report=state.fill_reports.get(job_id), cvs=cv_options(),
                               fillable=bool(p) and Route.from_dict(p["route"]).readable, stamp=prep_stamp(state, job_id),
                               running=bool(task and task.running), can_prep=config.module_on("bank"))

    @app.post("/job/<job_id>/notes")
    def job_notes(job_id: str) -> Any:
        state.job(job_id)
        state.db.set_job(job_id, notes=(request.get_json(force=True) or {}).get("notes") or "")
        return jsonify(ok=True)

    @app.post("/job/<job_id>/cv")
    def job_cv(job_id: str) -> Any:
        p = state.prep(job_id) or abort(404)
        variant = (request.get_json(force=True) or {}).get("variant") or ""
        path = cv_options().get(variant)
        if not path:
            return jsonify(ok=False, error=f"no CV variant {variant!r} (render them with `jr cv`)"), 400
        p.update(cv=path, cv_variant=variant, cv_why="picked by hand")
        save(p)
        state.db.set_job(job_id, cv_variant=variant)
        return jsonify(ok=True, variant=variant)

    @app.post("/job/<job_id>/prep")
    def job_prep(job_id: str) -> Any:
        state.job(job_id)
        return prep_task(state.prep_job_dict(job_id), "Prepping this job")

    @app.post("/prep-all")
    def prep_all() -> Any:
        force = (request.get_json(silent=True) or {}).get("force")
        picked = [jid for jid, e in state.statuses().items() if e["status"] == "picked" and jid in state.jobs]
        ids = [jid for jid in picked if force or not state.prep(jid)]
        if not ids:
            return jsonify(ok=False, error="every picked job is prepped already; use Re-prep all"), 400
        return started(*state.start(ALL, f"Prepping {len(ids)} job(s)", lambda t: state.prep_all(t, ids)),
                       total=len(ids))

    @app.post("/job/<job_id>/answer")
    def job_answer(job_id: str) -> Any:
        p = state.prep(job_id) or abort(404)
        data = request.get_json(force=True)
        it = next((x for x in p["items"] if x["key"] == data.get("key")), None) or abort(404)
        text = (data.get("text") or "").strip()
        company, role = p.get("company", ""), p.get("title", "")
        if data.get("scope") == "once" or data.get("option"):
            if data.get("option"):
                it["option"], it["answer"], it["status"] = data["option"], data["option"], "once"
            else:
                set_item_answer(it, text, "once")
            save(p)
            return jsonify(ok=True, status=it["status"])
        if not text:
            return jsonify(ok=False, error="empty answer"), 400
        with state.lock:
            a = prep_mod.approve_answer(state.db, state.db.bank(), it, text, company=company, kind=data.get("kind"))
        set_item_answer(it, render(text, company, role), {"story": "bank", "company": "adjust"}.get(a.kind, "auto"))
        save(p)
        return jsonify(ok=True, status=it["status"], answer_id=a.answer_id)

    @app.post("/job/<job_id>/questions")
    def job_questions(job_id: str) -> Any:
        """Questions copied by hand from a form the app can't read: re-prep the job with them (empty text clears)."""
        state.job(job_id)
        text = ((request.get_json(force=True) or {}).get("text") or "").strip()
        return prep_task(state.prep_job_dict(job_id),
                         "Answering the pasted questions" if text else "Clearing the pasted questions", pasted_text=text)

    @app.post("/job/<job_id>/apply-url")
    def job_apply_url(job_id: str) -> Any:
        """The real application form's URL, found by hand: prep from it (kept for later preps)."""
        state.job(job_id)
        url = ((request.get_json(force=True) or {}).get("url") or "").strip()
        if not url.startswith(("https://", "http://")):
            return jsonify(ok=False, error="paste a full https:// URL"), 400
        route = route_for_url(url, "pasted")
        state.db.set_job(job_id, apply_url=url)
        return prep_task(state.prep_job_dict(job_id), "Reading the form at this URL",
                         pasted=[] if route and route.readable else None)

    @app.post("/job/<job_id>/claude")
    def job_claude(job_id: str) -> Any:
        """Write an editing brief for the ticked answers and open it in the editor (or show the prompt to copy)."""
        job = state.job(job_id)
        p = state.prep(job_id) or abort(404)
        keys = [k for k in (request.get_json(force=True) or {}).get("keys") or []
                if any(it["key"] == k for it in p["items"])]
        if not keys:
            return jsonify(ok=False, error="tick at least one question first"), 400
        brief = claude_edit.write_brief(p, keys, state.db.bank(), job.get("description") or "")
        url = claude_edit.session_url(brief)
        return jsonify(ok=True, opened=claude_edit.open_session(url), url=url, brief=str(brief),
                       prompt=claude_edit.session_prompt(brief))

    @app.get("/job/<job_id>/stamp")
    def job_stamp(job_id: str) -> Any:
        return jsonify(stamp=prep_stamp(state, job_id))

    @app.post("/job/<job_id>/fill")
    def job_fill(job_id: str) -> Any:
        """What the Job Radar extension needs to open the form in the user's Chrome and fill it there."""
        p = state.prep(job_id) or abort(404)
        if not p.get("apply_url"):
            return jsonify(ok=False, error="no application form URL"), 400
        return jsonify(ok=True, payload=fill.payload(p, lambda text: cover_letter.write_pdf(job_id, text)))

    @app.post("/open-in-chrome")
    def app_open_in_chrome() -> Any:
        path = str((request.get_json(force=True) or {}).get("path") or "/")
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            abort(400)
        if not open_in_chrome(request.host_url.rstrip("/") + path):
            return jsonify(ok=False, error="Couldn't open Google Chrome: copy this page's address into it"), 500
        return jsonify(ok=True)

    @app.post("/job/<job_id>/fill-report")
    def job_fill_report(job_id: str) -> Any:
        p = state.prep(job_id) or abort(404)
        report = state.fill_reports[job_id] = fill.report(p, (request.get_json(force=True) or {}).get("report") or [])
        return jsonify(ok=True, filled=sum(r["ok"] for r in report), total=len(report),
                       unfilled=[r["label"] for r in report if not r["ok"]])

    @app.post("/job/<job_id>/cover-letter")
    def job_cover_letter(job_id: str) -> Any:
        p = state.prep(job_id) or abort(404)
        data = request.get_json(force=True) or {}
        it = next((x for x in p["items"] if x["key"] == data.get("key")), None) or abort(404)
        text = (data.get("text") or it.get("answer") or "").strip()
        if not text:
            return jsonify(ok=False, error="write the cover letter first"), 400
        if not _pdf_available():
            return jsonify(ok=False, error="Playwright is not installed (pip install 'jobradar-eu[full]')"), 501
        if text != (it.get("answer") or "").strip():
            set_item_answer(it, text, "once")
            save(p)
        path = cover_letter.write_pdf(job_id, text)
        return send_file(path, mimetype="application/pdf", as_attachment=True, download_name=path.name)

    @app.post("/job/<job_id>/applied")
    def job_applied(job_id: str) -> Any:
        """Mark applied: the decision event, plus (Full) the answers sent and bank usage counts."""
        state.job(job_id)
        p = state.prep(job_id)
        if p:
            sent = [] if p.get("prep") == "manual form" else p["items"]
            p["sent"] = {"at": _now(), "cv": p.get("cv"),
                         "answers": [{"label": it["label"], "value": p.get("cv") if it["status"] == "file" else
                                      sent_value(it)} for it in sent if it["status"] != "skip" and
                                     (sent_value(it) or it["status"] == "file")]}
            state.db.save_prep(p)
            used = [it["answer_id"] for it in sent if it.get("answer_id") and it["status"] in ("auto", "bank", "adjust")]
            state.db.count_use(sorted(set(used)))
            state.db.set_job(job_id, cv_variant=p.get("cv_variant"))
        state.decide(job_id, "applied")
        return jsonify(ok=True)

    # ---------------------------------------------------------------- bank, add, stats
    @app.get("/bank")
    def bank_page() -> str:
        flt = request.args.get("filter", "")
        rows = state.db.bank_rows()
        if flt == "draft":
            rows = [a for a in rows if a["status"] != "Approved"]
        elif flt == "empty":
            rows = [a for a in rows if not a["answer"].strip()]
        return render_template("bank.html", rows=rows, flt=flt)

    @app.post("/bank/<answer_id>")
    def bank_save(answer_id: str) -> Any:
        data = request.get_json(force=True) or {}
        cur = next((r for r in state.db.bank_rows() if r["answer_id"] == answer_id), None)
        if data.get("delete"):
            state.db.delete_answer(answer_id)
            return jsonify(ok=True, deleted=True)
        if cur is None and not data.get("question"):
            abort(404)
        answer = data.get("answer", (cur or {}).get("answer", ""))
        status = data.get("status") or (cur or {}).get("status") or "Draft"
        if status == "Approved" and not (answer or "").strip():
            return jsonify(ok=False, error="can't approve an empty answer"), 400
        aliases = data.get("aliases")
        if isinstance(aliases, str):
            aliases = [a.strip() for a in aliases.splitlines() if a.strip()]
        state.db.upsert_answer(answer_id, answer=answer, short=data.get("short"), status=status, aliases=aliases,
                               question=data.get("question"), kind=data.get("kind"))
        return jsonify(ok=True)

    @app.route("/add", methods=["GET", "POST"])
    def add() -> Any:
        if request.method == "GET":
            return render_template("add.html")
        data = request.get_json(force=True)
        url = (data.get("url") or "").strip()
        if not url.startswith("http"):
            return jsonify(ok=False, error="paste a full URL"), 400
        with state.http_factory() as http:
            found = read_posting(url, http)            # no LLM: JSON-LD / page text / ATS API
        title = (data.get("title") or "").strip() or found["title"]
        company = (data.get("company") or "").strip() or found["company"]
        if not title or not company:
            why = f" ({found['note']})" if found["note"] else ""
            return jsonify(ok=False, error=f"Couldn't read the role and company from that page{why}; type them in",
                           found={"title": title, "company": company}), 400
        job_id = state.db.add_manual_job(title=title, company=company, url=url, location=found["location"],
                                         description=found["description"], apply_url=found["route"].url or url)
        state.reload()
        status = "picked" if data.get("queue") == "apply" else "new"
        state.decide(job_id, status)
        if status == "picked" and config.module_on("bank"):
            prep_task(state.prep_job_dict(job_id), "Prepping the new job")
        return jsonify(ok=True, job_id=job_id, url=url_for("job_page", job_id=job_id), title=title, company=company,
                       route=found["route"].vendor, posting_text=len(found["description"]))

    @app.get("/stats")
    def stats() -> str:
        from jobradar.pipeline.weekly import weekly_stats

        data = None
        store = state.store()
        if store is not None:
            try:
                data = weekly_stats(_merged_decisions(store, state.db))
            finally:
                store.close()
        return render_template("stats.html", stats=data)

    @app.get("/favicon.ico")
    def favicon() -> Any:
        return redirect(url_for("static", filename="icon.svg"))

    return app


class _merged_decisions:
    """weekly_stats over state.db's shortlist plus this computer's decisions (some not pushed or imported yet)."""

    def __init__(self, store: Any, db: LocalDB):
        self.store, self.db = store, db

    def query(self, sql: str, params: Any = None, table: str | None = None) -> list[dict[str, Any]]:
        if "FROM decisions" in sql:
            return [{k: e.get(k) for k in ("job_id", "status", "at", "family", "source", "route")}
                    for e in self.db.conn.execute("SELECT * FROM events ORDER BY at").fetchall() for e in [dict(e)]]
        return self.store.query(sql, params, table=table)


def prep_stamp(state: AppState, job_id: str) -> str:
    r = state.db.conn.execute("SELECT updated_at FROM prep WHERE job_id = ?", (job_id,)).fetchone()
    return r[0] if r else ""


def _pdf_available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


def serve(port: int = 8765, *, open_path: str = "/", open_browser: bool = True, pull: bool = False) -> None:
    app = create_app(pull=pull)
    url = f"http://127.0.0.1:{port}{open_path}"
    print(f"Job Radar: {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
    finally:
        app.config["state"].push_now()
