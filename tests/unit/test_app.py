"""The local app (Flask test client) over a fixture state.db, local.db, recorded forms, a fake LLM and a fake sync:
morning view and decisions, prep, answers, the bank, adding jobs by hand, fill, stats."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("flask")

from fakes import FakeLLM  # noqa: E402
from jobradar.apply.prep import PrepHttp  # noqa: E402
from jobradar.http.client import ReplayTransport  # noqa: E402
from jobradar.llm.client import LLMClient  # noqa: E402

FORMS = Path(__file__).parents[1] / "fixtures" / "forms"
H = {"X-JR": "1"}
GITLAB_URL = "https://job-boards.greenhouse.io/gitlab/jobs/8860302002"


class FakeSync:
    """Stands in for jobradar.local.sync: records pushes, never touches GitHub."""

    def __init__(self):
        self.pushed: list[dict] = []
        self.fail = False

    def push_picks(self, db):
        if self.fail:
            raise RuntimeError("offline")
        events = db.unpushed()
        self.pushed += events
        db.mark_pushed([e["id"] for e in events])
        return {"pushed": len(events)}

    def pull_state(self, path):
        return {"bytes": 0}

    def pull_picks(self, db):
        return {"imported": 0}

    def workflow_runs(self, limit=3):
        return [{"status": "completed", "conclusion": "success", "url": "https://github.com/x/y/actions/runs/1"}]


def add_job(store, job_id, title, company, route_url, *, route="greenhouse", section="remote", fit=5, **extra):
    store.append("shortlist", [{"night_id": "n1", "night": date.today().isoformat(), "job_id": job_id, "title": title,
                                "company": company, "location_display": "Remote · Europe", "url": route_url,
                                "posting_url": route_url, "route_url": route_url, "route_vendor": route,
                                "section": section, "family": "data", "family_label": "", "fit": fit,
                                "summary": f"{company} builds things.", "sources": ["arbeitnow"],
                                "dedup_keys": [f"fz:{company.lower()}|{title.lower()}"], "why": "{}",
                                "description": f"{company} is hiring. We use dbt and BigQuery.", **extra}])


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    from jobradar.local.db import open_local
    from jobradar.settings import get_settings
    from jobradar.store.sqlite import SQLiteStore

    monkeypatch.setattr("jobradar.apply.prep.cv_choice",
                        lambda fam, title="": {"variant": "core", "path": "/cv/core.pdf", "why": "default"})
    store = SQLiteStore(get_settings().state_db)
    add_job(store, "JR-GITLAB", "Analytics Engineer", "GitLab", GITLAB_URL)
    add_job(store, "JR-SKIPME", "x", "y", "https://y.example/1", fit=2)
    store.append("nights", [{"night_id": "n1", "night": date.today().isoformat(), "kind": "daily", "status": "done",
                             "finished_at": "2026-11-03T02:20:00+00:00", "n_items": 2}])
    db = open_local()
    for aid, kind, q, ans, st in (("first_name", "fact", "First name", "Ana", "Approved"),
                                  ("email", "fact", "Email", "a@example.com", "Approved"),
                                  ("worked_here_before", "fact", "Have you previously worked at or consulted for {company}?",
                                   "No", "Approved"),
                                  ("visa_sponsorship", "fact", "Visa sponsorship", "", "Draft")):
        db.upsert_answer(aid, question=q, kind=kind, answer=ans, status=st)
    db.record("JR-GITLAB", "picked", title="Analytics Engineer", company="GitLab")
    db.record("JR-SKIPME", "skipped")
    visa = {"key": "question_38624172002", "answer_id": "visa_sponsorship", "kind": "fact", "canonical": "Visa"}
    llm_fake = FakeLLM(lambda p: {"items": [visa]} if "Answer bank" in p["messages"][-1]["content"]
                       else {"items": []} if "stored answer" in p["messages"][-1]["content"]
                       else {"facts": [], "instructions": []})
    sync = FakeSync()

    from jobradar.app.server import create_app

    app = create_app(db, llm_factory=lambda: LLMClient("prep", provider="ollama", model="m",
                                                       transport=llm_fake.transport(), sleep=False,
                                                       ledger_dir=tmp_path),
                     http_factory=lambda: PrepHttp(transport=ReplayTransport(FORMS / "greenhouse"), sleep=False),
                     syncer=sync)
    yield app.test_client(), store, db, sync
    state = app.config["state"]
    if state.push_timer is not None:
        state.push_timer.cancel()


def run(client, path, **body):
    """POST a slow action, wait for its background task and return the task (steps, result, error)."""
    r = client.post(path, headers=H, json=body).get_json()
    assert r["ok"], r
    client.application.config["state"].tasks[r["task"]].thread.join(30)
    t = client.get(f"/task/{r['task']}").get_json()
    assert not t["running"] and not t["error"], t
    return t


def reload(client):
    assert client.post("/reload", headers=H).get_json()["ok"]


def test_morning_view_decisions_and_post_guard(app_env):
    client, store, db, sync = app_env
    add_job(store, "JR-NEW1", "Data Engineer", "Acme", "https://acme.example/1", fit=4)
    add_job(store, "JR-NEW2", "Analytics Engineer", "Beta", "https://beta.example/1", fit=5, section="home")
    add_job(store, "JR-LOW", "Data Engineer", "Gamma", "https://g.example/1", fit=1, hidden="low_fit")
    reload(client)
    html = client.get("/").data.decode()
    assert html.index("Beta") > html.index("Acme")            # remote before near-home
    assert "Gamma" not in html and "show 1 low-fit" in html and "Last night: 2 jobs" in html
    assert "GitLab" not in html and "JR-SKIPME" not in html    # picked / skipped jobs are in their tabs
    assert "GitLab" in client.get("/?tab=picked").data.decode()
    assert "Gamma" in client.get("/?hidden=1").data.decode()
    assert "Beta" not in client.get("/?setup=remote").data.decode()
    assert client.post("/decide", headers=H, json={"job_id": "JR-NEW1", "status": "maybe"}).status_code == 400
    assert client.post("/decide", headers=H, json={"job_id": "JR-NEW1", "status": "skipped"}).get_json()["ok"]
    assert "Acme" not in client.get("/").data.decode() and "Acme" in client.get("/?tab=skipped").data.decode()
    ev = db.history("JR-NEW1")[-1]
    assert ev["status"] == "skipped" and ev["company"] == "Acme" and '"fz:acme|data engineer"' in ev["dedup_keys"]
    assert client.post("/sync", headers=H).get_json()["push"]["pushed"] >= 1        # Sync pushes at once
    assert any(e["job_id"] == "JR-NEW1" for e in sync.pushed) and not db.unpushed()
    assert client.post("/reload").status_code == 403                                 # no X-JR header
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403      # DNS rebinding


def test_decisions_are_pushed_in_a_batch_after_a_quiet_moment(app_env):
    client, _, db, sync = app_env
    state = client.application.config["state"]
    client.post("/decide", headers=H, json={"job_id": "JR-SKIPME", "status": "later"})
    assert state.push_timer is not None and not sync.pushed                           # not at once
    state.push_timer.cancel()
    sync.fail = True
    state.push_now()
    assert state.sync_status["ok"] is False and db.unpushed()                         # kept for the next try
    assert "sync failed" in client.get("/").data.decode()
    sync.fail = False
    client.post("/close", headers=H)                                                  # page closing: pushed
    assert not db.unpushed()


def test_stages_after_applying_and_stats(app_env):
    client, store, db, _ = app_env
    client.post("/decide", headers=H, json={"job_id": "JR-GITLAB", "status": "applied"})
    client.post("/decide", headers=H, json={"job_id": "JR-GITLAB", "status": "interview"})
    html = client.get("/?tab=applied").data.decode()
    assert "GitLab" in html and "<option selected>interview</option>" in html
    stats = client.get("/stats").data.decode()
    assert "<b>1</b> applied" in stats and "<b>1</b> interviews" in stats and "arbeitnow" in stats


def test_lite_job_page_has_no_prep(app_env, monkeypatch):
    client, _, _, _ = app_env
    monkeypatch.setattr("jobradar.config.module_on", lambda name: False)
    html = client.get("/job/JR-GITLAB").data.decode()
    assert "Lite mode: open the posting" in html and "prepOne" not in html and "GitLab builds things." in html


def test_extension_ping_is_recorded(app_env):
    client, _, db, _ = app_env
    assert client.post("/ext/seen", headers=H).get_json()["ok"] and db.get_meta("extension_seen")


def test_prep_answer_and_mark_applied(app_env):
    client, store, db, _ = app_env
    t = run(client, "/job/JR-GITLAB/prep")
    steps = [s["msg"] for s in t["steps"]]
    assert steps[:3] == ["Finding the application form", "Reading the greenhouse form",
                         f"Matching {t['result']['questions']} questions to your answer bank"]
    assert steps[-1] == "Saving" and t["result"]["prep"] == "needs answers"
    assert db.job("JR-GITLAB")["prep"] == "needs answers"
    html = client.get("/job/JR-GITLAB").data.decode()
    assert "Needs your answer" in html and "Have you previously worked" in html and "ready to copy" in html
    assert html.count('onclick="copyAnswer(this)"') >= 2
    visa_key = "question_38624172002"
    r = client.post("/job/JR-GITLAB/answer", headers=H, json={"key": visa_key, "text": "No", "scope": "bank"})
    assert r.get_json()["status"] == "auto"
    rows = {r["answer_id"]: r for r in db.bank_rows()}
    assert rows["visa_sponsorship"]["status"] == "Approved" and rows["visa_sponsorship"]["answer"] == "No"
    assert any("sponsorship for a visa" in a for a in rows["visa_sponsorship"]["aliases"])
    client.post("/job/JR-GITLAB/answer", headers=H, json={"key": "first_name", "text": "Anita", "scope": "once"})
    assert {r["answer_id"]: r for r in db.bank_rows()}["first_name"]["answer"] == "Ana"
    assert client.post("/job/JR-GITLAB/applied", headers=H).get_json()["ok"]
    assert db.status("JR-GITLAB") == "applied"
    sent = db.prep("JR-GITLAB")["sent"]
    values = [a["value"] for a in sent["answers"]]
    assert "Anita" in values and "No" in values and sent["cv"] == "/cv/core.pdf"
    rows = {r["answer_id"]: r for r in db.bank_rows()}
    assert rows["visa_sponsorship"]["uses"] == 1 and rows["first_name"]["uses"] == 0   # override not counted
    assert "GitLab" not in client.get("/?tab=picked").data.decode()


def test_job_page_splits_posting_instructions_from_warnings(app_env):
    from jobradar.apply.prep import load_cached, save_cached

    client, _, _, _ = app_env
    run(client, "/job/JR-GITLAB/prep")
    p = load_cached("JR-GITLAB")
    p["warnings"] = ["Posting instruction: mention code BLUE", "No CV path"]
    save_cached(p)
    html = client.get("/job/JR-GITLAB").data.decode()
    assert "mention code BLUE" in html and "No CV path" in html


def test_bank_editor_and_add(app_env):
    client, _, db, _ = app_env
    assert b"visa_sponsorship" in client.get("/bank?filter=draft").data
    r = client.post("/bank/visa_sponsorship", headers=H, json={"answer": "", "status": "Approved"})
    assert r.status_code == 400                                       # can't approve an empty answer
    r = client.post("/bank/visa_sponsorship", headers=H, json={"answer": "No", "status": "Approved",
                                                                  "aliases": "do you need a visa\nvisa?"})
    row = {r["answer_id"]: r for r in db.bank_rows()}["visa_sponsorship"]
    assert r.get_json()["ok"] and row["status"] == "Approved" and row["aliases"] == ["do you need a visa", "visa?"]
    assert client.post("/bank/visa_sponsorship", headers=H, json={"delete": True}).get_json()["deleted"]
    assert "visa_sponsorship" not in {r["answer_id"] for r in db.bank_rows()}
    r = client.post("/add", headers=H, json={"url": GITLAB_URL, "company": "GitLab", "title": "AE", "queue": "apply"})
    d = r.get_json()
    assert d["ok"] and d["job_id"].startswith("M-") and db.status(d["job_id"]) == "picked"
    client.application.config["state"].tasks[d["job_id"]].thread.join(30)     # prepped in the background
    assert db.job(d["job_id"])["prep"]
    assert "GitLab" in client.get("/?tab=picked").data.decode()


def test_add_by_hand_goes_to_new_without_ai(app_env):
    client, _, db, _ = app_env
    url = GITLAB_URL + "?src=me"
    r = client.post("/add", headers=H, json={"url": url})        # the page isn't recorded: no role to read
    assert r.status_code == 400 and r.get_json()["found"]["company"] == "Gitlab"
    d = client.post("/add", headers=H, json={"url": url, "title": "Analytics Engineer"}).get_json()
    assert d["ok"] and d["route"] == "greenhouse" and d["posting_text"] > 500
    job = db.job(d["job_id"])
    assert job["manual"] == 1 and "GitLab" in job["description"] and db.status(d["job_id"]) == "new"
    assert d["job_id"] not in client.application.config["state"].tasks           # no prep, no LLM
    assert "Analytics Engineer" in client.get("/").data.decode()


def test_reprep_keeps_answers_set_for_this_application():
    from jobradar.app.server import keep_own_answers

    old = {"items": [{"key": "q1", "label": "Why us?", "status": "once", "answer": "Mine", "option": ""},
                     {"key": "q2", "label": "Changed label", "status": "once", "answer": "x", "option": ""},
                     {"key": "q3", "label": "City", "status": "auto", "answer": "Old", "option": ""}]}
    new = {"items": [{"key": "q1", "label": "Why us?", "status": "adjust", "answer": "AI draft", "drafted": True},
                     {"key": "q2", "label": "Other question", "status": "missing", "answer": ""},
                     {"key": "q3", "label": "City", "status": "auto", "answer": "New"}]}
    assert keep_own_answers(new, old) == 1
    assert new["items"][0] == {"key": "q1", "label": "Why us?", "status": "once", "answer": "Mine", "option": "",
                               "drafted": False}
    assert new["items"][1]["status"] == "missing" and new["items"][2]["answer"] == "New"


def test_cv_picked_by_hand_survives_reprep_and_is_recorded(app_env, monkeypatch):
    client, store, db, _ = app_env
    monkeypatch.setattr("jobradar.app.server.cv_options",
                        lambda: {"core": "/cv/core.pdf", "ai": "/cv/ai.pdf"})
    run(client, "/job/JR-GITLAB/prep")
    html = client.get("/job/JR-GITLAB").data.decode()
    assert 'id="cv-variant"' in html and "/cv/core.pdf" in html
    assert client.post("/job/JR-GITLAB/cv", headers=H, json={"variant": "nope"}).status_code == 400
    assert client.post("/job/JR-GITLAB/cv", headers=H, json={"variant": "ai"}).get_json()["ok"]
    run(client, "/job/JR-GITLAB/prep")                                 # re-prep keeps the hand-picked CV
    assert "picked by hand · /cv/ai.pdf" in client.get("/job/JR-GITLAB").data.decode()
    client.post("/job/JR-GITLAB/applied", headers=H)
    assert db.job("JR-GITLAB")["cv_variant"] == "ai" and db.prep("JR-GITLAB")["sent"]["cv"] == "/cv/ai.pdf"


def test_unreadable_form_paste_claude_brief_and_jr_answer(app_env, monkeypatch, tmp_path):
    from jobradar.apply.prep import load_cached
    from jobradar.cli import main

    client, store, db, _ = app_env
    add_job(store, "JR-LINKED", "Senior Data Engineer", "emagine", "https://www.linkedin.com/jobs/view/4012345678",
            route="linkedin")
    db.record("JR-LINKED", "picked")
    reload(client)
    assert run(client, "/job/JR-LINKED/prep")["result"]["prep"] == "manual form"
    html = client.get("/job/JR-LINKED").data.decode()
    assert "Copy posting link" in html and "linkedin.com/jobs/view/4012345678" in html
    assert "Auto-fill in browser" not in html and "can't read this application form" in html
    assert 'class="q ' not in html and "Cover letter" not in html                  # no answers without questions
    run(client, "/job/JR-GITLAB/prep")
    gitlab = client.get("/job/JR-GITLAB").data.decode()
    assert "Auto-fill in browser" in gitlab and "Copy posting link" in gitlab and "can't read this" not in gitlab

    # pasted questions: the fake LLM can't parse, so one question per line; bank answers are matched
    paste = "Have you previously worked at or consulted for emagine?\nRequired\nWhy are you a good fit?"
    t = run(client, "/job/JR-LINKED/questions", text=paste)
    assert t["result"]["questions"] == 2 and t["steps"][0]["msg"] == "Splitting the pasted text into questions"
    p = load_cached("JR-LINKED")
    by = {it["label"]: it for it in p["items"]}
    assert by["Have you previously worked at or consulted for emagine?"]["answer"] == "No"
    assert len(p["pasted"]) == 2
    run(client, "/job/JR-LINKED/prep")                                  # re-prep keeps the pasted questions
    assert len(load_cached("JR-LINKED")["pasted"]) == 2

    # the Claude brief holds the ticked questions and the save command; the editor link carries its path
    opened = []
    monkeypatch.setenv("JR_EDITOR", "cursor")
    monkeypatch.setattr("jobradar.apply.claude_edit.open_session", lambda url: opened.append(url) or True)
    fit = by["Why are you a good fit?"]["key"]
    assert client.post("/job/JR-LINKED/claude", headers=H, json={"keys": []}).status_code == 400
    r = client.post("/job/JR-LINKED/claude", headers=H, json={"keys": [fit]}).get_json()
    assert r["opened"] and opened[0].startswith("cursor://anthropic.claude-code/open?prompt=")
    brief = Path(r["brief"]).read_text(encoding="utf-8")
    assert "Why are you a good fit?" in brief and f"key: `{fit}`" in brief and "jr answer JR-LINKED --key" in brief
    assert "Have you previously worked" not in brief

    # jr answer (what Claude runs) saves for this application; the page sees the newer stamp
    db.conn.execute("UPDATE prep SET updated_at = '2000-01-01T00:00:00+00:00' WHERE job_id = 'JR-LINKED'")
    db.conn.commit()
    old = client.get("/job/JR-LINKED/stamp").get_json()["stamp"]
    assert main(["answer", "JR-LINKED", "--key", fit, "--text", "I have built dbt models in BigQuery."]) == 0
    item = next(it for it in load_cached("JR-LINKED")["items"] if it["key"] == fit)
    assert item["status"] == "once" and item["edited"] == "editor" and item["answer"].startswith("I have built")
    assert client.get("/job/JR-LINKED/stamp").get_json()["stamp"] > old
    assert "edited in your editor" in client.get("/job/JR-LINKED").data.decode()
    assert main(["answer", "JR-LINKED", "--key", "nope", "--text", "x"]) == 1

    # the real form's URL, found by hand behind LinkedIn: the job is prepped from that form and can be filled
    assert client.post("/job/JR-LINKED/apply-url", headers=H, json={"url": "not a url"}).status_code == 400
    r = run(client, "/job/JR-LINKED/apply-url", url="https://job-boards.greenhouse.io/gitlab/jobs/8860302002")["result"]
    assert r["vendor"] == "greenhouse" and r["prep"] != "manual form"
    p = load_cached("JR-LINKED")
    assert "pasted" not in p and any(it["status"] == "file" for it in p["items"])
    assert "greenhouse.io" in db.job("JR-LINKED")["apply_url"]
    assert "Auto-fill in browser" in client.get("/job/JR-LINKED").data.decode()


def test_unprepped_job_page_asks_for_an_unreadable_form_straight_away(app_env):
    from jobradar.apply.prep import load_cached

    client, store, db, _ = app_env
    add_job(store, "JR-LINKED", "Data Engineer", "emagine", "https://www.linkedin.com/jobs/view/7", route="linkedin")
    add_job(store, "JR-UNKNOWN", "Data Engineer", "Acme", "https://careers.example.com/jobs/7", route="unknown")
    reload(client)

    linked = client.get("/job/JR-LINKED").data.decode()                # LinkedIn: known from the link, no Prep needed
    assert "can't read this application form" in linked and 'id="pasted"' in linked and 'id="apply-url"' in linked
    assert "prepOne(this)" not in linked and "Not prepped yet" not in linked
    unknown = client.get("/job/JR-UNKNOWN").data.decode()              # unknown: paste, or let Prep look for a form
    assert "doesn't know where this application form is" in unknown and 'id="pasted"' in unknown
    assert "Look for a readable form" in unknown
    gitlab = client.get("/job/JR-GITLAB").data.decode()                # readable: Prep as before
    assert "Not prepped yet" in gitlab and ">Prep</button>" in gitlab and 'id="pasted"' not in gitlab

    t = run(client, "/job/JR-LINKED/questions", text="Why are you a good fit?")   # pasting works without a Prep
    assert t["result"]["questions"] == 1 and len(load_cached("JR-LINKED")["pasted"]) == 1


def test_mark_applied_on_manual_form_skips_the_fact_cheat_sheet(app_env):
    client, store, db, _ = app_env
    add_job(store, "JR-MANUAL", "Data Engineer", "Acme", "https://www.linkedin.com/jobs/view/42", route="linkedin")
    reload(client)
    assert run(client, "/job/JR-MANUAL/prep")["result"]["prep"] == "manual form"
    assert client.post("/job/JR-MANUAL/applied", headers=H).get_json()["ok"]
    assert db.status("JR-MANUAL") == "applied" and db.prep("JR-MANUAL")["sent"]["answers"] == []
    assert {r["answer_id"]: r for r in db.bank_rows()}["email"]["uses"] == 0         # nothing counted as used


def test_prep_all_reports_each_job_and_pasted_cover_letter_downloads_as_pdf(app_env, monkeypatch):
    from jobradar.apply.prep import load_cached

    client, store, db, _ = app_env
    add_job(store, "JR-LINKED", "Data Engineer", "emagine", "https://www.linkedin.com/jobs/view/1", route="linkedin")
    db.record("JR-LINKED", "picked")
    reload(client)
    t = run(client, "/prep-all")
    assert t["result"] == {"ready": 1, "manual": 1, "failed": 0} and t["done"] == t["total"] == 2
    outcomes = {s["kind"]: s["msg"] for s in t["steps"]}
    assert "questions read and answered" in outcomes["ok"] and "add its questions" in outcomes["warn"]
    queue = client.get("/?tab=picked").data.decode()
    assert "Prep all" in queue and "RUNNING = false" in queue
    assert client.post("/prep-all", headers=H).status_code == 400              # nothing left to prep

    # a pasted cover-letter question → the edited text comes back as a PDF and is kept for this application
    run(client, "/job/JR-LINKED/questions", text="Cover letter")
    item = load_cached("JR-LINKED")["items"][0]
    assert item["status"] == "missing" and "Download as PDF" not in client.get("/job/JR-LINKED").data.decode()
    client.post("/job/JR-LINKED/answer", headers=H, json={"key": item["key"], "text": "Hi", "scope": "once"})
    assert "Download as PDF" in client.get("/job/JR-LINKED").data.decode()
    monkeypatch.setattr("jobradar.apply.cover_letter.to_pdf_bytes", lambda html: b"%PDF-" + html.encode())
    monkeypatch.setattr("jobradar.apply.cover_letter.letterhead",
                        lambda: {"name": "Ana Ferreira", "contact": [["a@example.com"]]})
    assert client.post("/job/JR-LINKED/cover-letter", headers=H, json={"key": "nope", "text": "x"}).status_code == 404
    r = client.post("/job/JR-LINKED/cover-letter", headers=H,
                    json={"key": item["key"], "text": "Hi emagine team,\n\nI build dbt models."})
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert "Ana_Ferreira_Cover_Letter.pdf" in r.headers["Content-Disposition"]
    body = r.data.decode()
    assert "I build dbt models." in body and "Best regards," in body and "a@example.com" in body
    saved = load_cached("JR-LINKED")["items"][0]
    assert saved["status"] == "once" and saved["answer"].startswith("Hi emagine team")


def test_task_already_running_is_reused(app_env):
    import threading

    client, store, db, _ = app_env
    state = client.application.config["state"]
    gate = threading.Event()
    task, new = state.start("JR-GITLAB", "slow", lambda t: gate.wait(5) and {})
    assert new
    r = client.post("/job/JR-GITLAB/prep", headers=H).get_json()
    assert r["ok"] and r["already"] and r["task"] == "JR-GITLAB"
    assert client.get("/task/JR-GITLAB").get_json()["what"] == "slow"
    assert "RUNNING = true" in client.get("/job/JR-GITLAB").data.decode()
    gate.set()
    task.thread.join(5)
    assert client.get("/task/nope").get_json()["missing"]


def test_cover_letter_file_name_and_closing():
    from jobradar.apply.cover_letter import file_name, render_html

    assert file_name("Łucja Øster") == "Lucja_Oster_Cover_Letter.pdf" and file_name("") == "Cover_Letter.pdf"
    head = {"name": "Łucja Øster", "contact": []}
    assert "Best regards," in render_html("Hi team,\n\nThank you for your time.", head)
    signed = render_html("Hi team,\n\nI like data.\n\nKind regards,\nŁucja", head)
    assert signed.count("regards") == 1 and "Łucja Øster" in signed            # no second closing


def test_fill_payload_for_the_extension_and_its_report(app_env, tmp_path):
    from jobradar.apply.prep import load_cached, save_cached

    client, store, db, _ = app_env
    run(client, "/job/JR-GITLAB/prep")
    p = load_cached("JR-GITLAB")
    cv = tmp_path / "Ana_Ferreira_CV.pdf"
    cv.write_bytes(b"%PDF cv")
    save_cached({**p, "cv": str(cv)})
    html = client.get("/job/JR-GITLAB").data.decode()
    assert 'id="autofill"' in html and 'id="ext-missing"' not in html      # no note: other browsers hand off to Chrome
    assert client.post("/job/JR-GITLAB/fill").status_code == 403             # POSTs need X-JR
    out = client.post("/job/JR-GITLAB/fill", headers=H).get_json()
    payload = out["payload"]
    assert out["ok"] and payload["job_id"] == "JR-GITLAB" and payload["url"] == p["apply_url"]
    assert payload["items"] and all(it["status"] in ("auto", "bank", "adjust", "once", "file") for it in payload["items"])
    resume = next(it for it in payload["items"] if it["status"] == "file")
    assert resume["file"]["name"] == "Ana_Ferreira_CV.pdf"

    first = payload["items"][0]
    r = client.post("/job/JR-GITLAB/fill-report", headers=H,
                    json={"report": [{"key": first["key"], "ok": True, "how": "name: text"}]}).get_json()
    assert r["ok"] and r["filled"] == 1 and r["total"] >= len(payload["items"])
    html = client.get("/job/JR-GITLAB").data.decode()
    assert f"Fill report: 1/{r['total']} filled" in html and "Check everything in the form's tab" in html


@pytest.mark.parametrize("how", ["click", "opened from another browser"])
def test_auto_fill_button_through_the_extension(app_env, tmp_path, how):
    """The job page's button (or ?autofill=1, the page Auto-fill opened in Chrome from Cursor's browser), app.js, the
    unpacked extension and the fill report, against a local form."""
    import http.server
    import threading

    from werkzeug.serving import make_server

    from jobradar.apply.prep import load_cached, save_cached
    from test_v2_fill import EXT, _Pages

    sync_api = pytest.importorskip("playwright.sync_api")
    client, store, db, _ = app_env
    run(client, "/job/JR-GITLAB/prep")
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    forms = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Pages)
    for s in (server, forms):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    base = f"http://localhost:{server.server_port}"
    form_url = f"http://127.0.0.1:{forms.server_address[1]}/form"
    save_cached({**load_cached("JR-GITLAB"), "apply_url": form_url})
    try:
        with sync_api.sync_playwright() as pw:
            try:
                ctx = pw.chromium.launch_persistent_context(
                    str(tmp_path / "profile"), headless=True, channel="chromium",
                    args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"])
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"Chromium with extensions unavailable ({type(exc).__name__})")
            try:
                page = ctx.new_page()
                if how == "click":
                    page.goto(f"{base}/job/JR-GITLAB")
                    page.wait_for_function("document.documentElement.dataset.jrFiller", timeout=10_000)
                    page.get_by_role("button", name="Auto-fill in browser").click()
                else:
                    page.goto(f"{base}/job/JR-GITLAB?autofill=1")               # fills without a click
                page.wait_for_selector("section.autofill", timeout=30_000)       # the page reloads with the report
                assert "Fill report:" in page.inner_text("section.autofill")
                assert page.url == f"{base}/job/JR-GITLAB"                       # a reload won't fill again
                form = next(p for p in ctx.pages if p.url == form_url)
                assert form.evaluate("[document.querySelector('#first_name').value, !!window.submitted]") == ["Ana", False]
            finally:
                ctx.close()
    finally:
        server.shutdown()
        forms.shutdown()
    report = client.application.config["state"].fill_reports["JR-GITLAB"]
    assert any(r["key"] == "first_name" and r["ok"] for r in report)


def test_open_in_chrome_only_opens_app_pages(app_env, monkeypatch):
    client, store, db, _ = app_env
    opened = []
    monkeypatch.setattr("jobradar.app.server.open_in_chrome", lambda url: opened.append(url) or True)
    assert client.post("/open-in-chrome", json={"path": "/job/JR-GITLAB"}).status_code == 403   # POSTs need X-JR
    assert client.post("/open-in-chrome", headers=H, json={"path": "/job/JR-GITLAB"}).get_json()["ok"]
    assert opened == ["http://localhost/job/JR-GITLAB"]
    for bad in ("https://example.com/", "//example.com/x", "/\\example.com"):
        assert client.post("/open-in-chrome", headers=H, json={"path": bad}).status_code == 400
    assert len(opened) == 1
    monkeypatch.setattr("jobradar.app.server.open_in_chrome", lambda url: False)
    r = client.post("/open-in-chrome", headers=H, json={"path": "/"})
    assert r.status_code == 500 and "Chrome" in r.get_json()["error"]


def test_auto_fill_outside_chrome_opens_the_page_in_chrome(app_env, monkeypatch):
    """No extension here (Cursor's built-in browser): Auto-fill opens this job page in Chrome to fill there, no note.
    In a Chrome without the extension it says so instead of opening another Chrome tab."""
    import threading

    from werkzeug.serving import make_server

    sync_api = pytest.importorskip("playwright.sync_api")
    client, store, db, _ = app_env
    run(client, "/job/JR-GITLAB/prep")
    opened = []
    monkeypatch.setattr("jobradar.app.server.open_in_chrome", lambda url: opened.append(url) or True)
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://localhost:{server.server_port}"
    cursor_ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Cursor/2.0.0 "
                 "Chrome/138.0.0.0 Electron/37.0.0 Safari/537.36")
    chrome = ("Object.defineProperty(navigator, 'userAgentData', "
              "{value: {brands: [{brand: 'Google Chrome', version: '156'}, {brand: 'Chromium', version: '156'}]}})")
    try:
        with sync_api.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"Chromium unavailable ({type(exc).__name__})")
            try:
                ctx = browser.new_context(user_agent=cursor_ua)
                page = ctx.new_page()
                page.goto(f"{base}/job/JR-GITLAB")
                page.get_by_role("button", name="Auto-fill in browser").click()
                page.wait_for_selector("#toast.show", timeout=5_000)
                assert opened == [f"{base}/job/JR-GITLAB?autofill=1"]
                assert "Opened in Chrome" in page.inner_text("#toast")
                ctx.close()

                ctx = browser.new_context()
                ctx.add_init_script(chrome)
                page = ctx.new_page()
                page.goto(f"{base}/job/JR-GITLAB")
                page.get_by_role("button", name="Auto-fill in browser").click()
                page.wait_for_selector("#toast.show.bad", timeout=5_000)
                assert "isn't on in this Chrome" in page.inner_text("#toast") and len(opened) == 1
                ctx.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
