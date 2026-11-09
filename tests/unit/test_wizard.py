"""Setup wizard: every step through the Flask pages with a fake `gh`, a fake LLM test and a temp home; both modes
end with the doctor checks green."""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("flask")

from conftest import use_profile  # noqa: E402

H = {"X-JR": "1"}
TEMPLATE_WF = (Path(__file__).parents[2] / "src/jobradar/defaults/config_repo/jobradar.yml").read_text(encoding="utf-8")

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_GH_LOG"]
args = sys.argv[1:]
with open(log, "a") as f:
    f.write(json.dumps(args) + "\n")
state = os.environ["FAKE_GH_STATE"]
created = os.path.exists(state)
def out(x): sys.stdout.write(x if isinstance(x, str) else json.dumps(x)); sys.exit(0)
if args[:2] == ["auth", "status"]:
    sys.stdout.write("github.com\n  Logged in to github.com account ana-demo (keyring)\n  - Token scopes: 'gist', 'read:org', 'repo', 'workflow'\n"); sys.exit(0)
if args[:2] == ["repo", "view"]:
    sys.exit(0 if created else 1)
if args[:2] == ["repo", "create"]:
    open(state, "w").write("1"); sys.exit(0)
if args[:2] in (["secret", "set"], ["workflow", "enable"], ["workflow", "run"]):
    sys.exit(0)
if args[:2] == ["run", "list"]:
    out([{"status": "completed", "conclusion": "success", "url": "https://github.com/x"}])
if args[0] == "api":
    path = args[-1] if not args[-1].startswith("-") and args[-1] != "-" else next(a for a in args if a.startswith("repos/"))
    path = next((a for a in args if a.startswith("repos/") or a in ("user",)), path)
    if path == "user":
        out({"login": "ana-demo"})
    if "/settings/billing/" in path:
        out({"total_minutes_used": 120, "included_minutes": 2000})
    if "-X" in args:
        out({})
    if "contents/.github/workflows/jobradar.yml" in path:
        out(open(os.environ["FAKE_GH_WF"]).read())
    if "/branches/picks" in path:
        out({"name": "picks"})
    sys.stderr.write("HTTP 404: Not Found"); sys.exit(1)
sys.stderr.write("unknown " + " ".join(args)); sys.exit(2)
'''


@pytest.fixture
def wiz(tmp_path, monkeypatch):
    user = tmp_path / "user"
    user.mkdir()
    use_profile(monkeypatch, user)
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(FAKE_GH.replace("#!/usr/bin/env python3", f"#!{sys.executable}"), encoding="utf-8")
    gh.chmod(0o755)
    if sys.platform == "win32":                     # Windows can't run a #! script: call it through a .cmd wrapper
        (gh.parent / "gh.cmd").write_text(f'@"{sys.executable}" "{gh}" %*\r\n', encoding="utf-8")
        gh = gh.parent / "gh.cmd"
    wf = tmp_path / "wf.yml"
    wf.write_text(TEMPLATE_WF, encoding="utf-8")
    monkeypatch.setenv("JR_GH", str(gh))
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "gh.created"))
    monkeypatch.setenv("FAKE_GH_WF", str(wf))
    for k in ("MISTRAL_API_KEY", "JR_CONFIG_REPO"):
        monkeypatch.delenv(k, raising=False)
    for k in ("GEMINI_API_KEY", "NOTION_TOKEN", "NOTION_JOBS_DB", "NOTION_PARENT_PAGE", "NOTION_JOBS_URL"):
        monkeypatch.setenv(k, "")              # the wizard sets these in os.environ: blanked, then restored after
    from jobradar.app.server import create_app
    from jobradar.local.db import open_local

    app = create_app(open_local(), syncer=None)
    yield app.test_client(), tmp_path


def step(client, sid, **data):
    r = client.post(f"/setup/{sid}", headers=H, json=data)
    body = r.get_json()
    assert body["ok"], (sid, body)
    return body


def gh_calls(tmp_path):
    return [json.loads(x) for x in (tmp_path / "gh.log").read_text(encoding="utf-8").splitlines()]


BASICS = {"city": "Lisbon", "country": "PT", "work_setups": ["remote_eu", "remote_country", "hybrid"],
          "hybrid_radius_km": "30", "languages": {"pt": "native", "en": "C1", "es": "B1"},
          "contracts": ["employment"], "timezone": "Europe/Lisbon", "run_at": "05:30", "salary": "55k EUR",
          "work_countries": [], "relocate_to": []}


def _night():
    from jobradar.settings import get_settings
    from jobradar.store.sqlite import SQLiteStore

    store = SQLiteStore(get_settings().state_db)
    store.append("nights", [{"night_id": "n", "night": date.today(), "kind": "daily", "status": "done",
                             "finished_at": datetime.now(timezone.utc), "n_items": 3}])
    store.close()


class FakeNotionClient:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def init_jobs(self, parent):
        return {"database": "db1", "data_source": "ds1", "url": "https://www.notion.so/db1"}


NOTION = {"token": "ntn_test", "page": "https://www.notion.so/Job-search-0123456789abcdef0123456789abcdef"}


def _llm_ok(monkeypatch):
    from jobradar import wizard

    monkeypatch.setattr(wizard, "test_llm", lambda data: {"ok": True, "model": "gemini-3.5-flash-lite",
                                                          "provider": "openai_compatible", "seconds": 0.1})
    monkeypatch.setattr(wizard, "notion_client", FakeNotionClient)


def test_radar_walkthrough_ends_green(wiz, monkeypatch):
    from jobradar import wizard

    client, tmp = wiz
    _llm_ok(monkeypatch)
    assert client.get("/setup/").headers["Location"].endswith("/setup/welcome")
    assert "Radar + Apply" in client.get("/setup/welcome").data.decode()
    step(client, "welcome", mode="radar")
    assert "Connected" in client.get("/setup/github").data.decode() and "ana-demo" in client.get("/setup/github").data.decode()
    step(client, "github")
    r = client.post("/setup/basics", headers=H, json={**BASICS, "city": ""})
    assert r.status_code == 400 and "required" in r.get_json()["error"]
    step(client, "basics", **BASICS)
    from jobradar import config

    loc = config.profile()["location"]
    assert loc["lat"] and loc["hybrid_radius_km"] == 30 and config.profile()["languages"]["en"] == "C1"
    assert "55k" not in (Path(os.environ["JR_USER_DIR"]) / "profile.yaml").read_text(encoding="utf-8")          # salary stays local
    preview = client.post("/setup/api/roles-preview", headers=H, json={"packs": ["backend"], "min": "junior",
                                                                         "max": "senior"}).get_json()
    kept = {x["title"] for x in preview["kept"]}
    assert "Backend Engineer" in kept and "Head of Engineering" not in kept
    assert client.post("/setup/roles", headers=H, json={"packs": []}).status_code == 400
    step(client, "roles", packs=["backend"], min="junior", max="senior", never=["php"], must_have=[], extra_titles=[])
    html = client.get("/setup/countries_sources").data.decode()
    assert "Countries and job boards" in html
    step(client, "countries_sources", countries=["PT", "ES"], opt_in=[], disable=[])
    assert client.post("/setup/llm", headers=H, json={"skip": True}).status_code == 400          # required now
    step(client, "llm", provider="gemini", key="AIza-test", model="")
    assert config.llm_config()["default"] == {"provider": "gemini"}                               # model picked at run time
    assert client.post("/setup/notion", headers=H, json={"token": "", "page": ""}).status_code == 400
    step(client, "notion", **NOTION)
    env = wizard.read_env()
    assert env["NOTION_JOBS_DB"] == "ds1" and env["NOTION_PARENT_PAGE"] == "0123456789abcdef0123456789abcdef"
    for s in ("experience", "optional_keys"):                                         # optional in Radar
        assert step(client, s, skip=True)["next"]
    res = step(client, "config_repo", name="job-radar-config")["result"]
    assert res["repo"] == "ana-demo/job-radar-config" and "user/profile.yaml" in res["pushed"]
    assert set(res["secrets"]) == {"GEMINI_API_KEY", "NOTION_TOKEN", "NOTION_JOBS_DB", "NOTION_PARENT_PAGE",
                                   "NOTION_JOBS_URL"}
    calls = gh_calls(tmp)
    create = next(c for c in calls if c[:2] == ["repo", "create"])
    assert create[:4] == ["repo", "create", "ana-demo/job-radar-config", "--private"] and "--template" not in create
    puts = [c[3] for c in calls if c[:3] == ["api", "-X", "PUT"]]
    assert any(p.endswith("contents/.github/workflows/jobradar.yml") for p in puts)   # the package's workflow
    assert any(p.endswith("contents/README.md") for p in puts)
    assert not any("picks" in " ".join(c) for c in calls)                             # no picks branch any more
    assert config.user_settings()["config_repo"] == "ana-demo/job-radar-config"
    assert config.mode() == "radar" and config.module_on("ats") and config.module_on("screen")
    assert not config.module_on("bank") and not config.module_on("app")
    # changing a step later pushes that file again
    step(client, "roles", packs=["backend", "fullstack"], min="junior", max="senior", never=[], must_have=[],
         extra_titles=[])
    assert any(c[:3] == ["api", "-X", "PUT"] and c[3].endswith("contents/user/roles.yaml") for c in gh_calls(tmp)[len(calls):])
    _night()
    step(client, "first_run")
    html = client.get("/setup/summary").data.decode()
    assert "All set" in html, html


def test_apply_walkthrough_ends_green(wiz, monkeypatch):
    from jobradar import wizard

    client, tmp = wiz
    _llm_ok(monkeypatch)
    step(client, "welcome", mode="full")                                   # the old name still works
    step(client, "github")
    step(client, "basics", **BASICS)
    step(client, "roles", packs=["backend", "devops-sre-platform"], min="junior", max="senior", never=[],
         must_have=[], extra_titles=[])
    step(client, "countries_sources", countries=["PT"], opt_in=[], disable=[])
    monkeypatch.setattr(wizard, "test_llm", lambda data: {"ok": True, "model": "ministral-14b-latest",
                                                          "provider": "openai_compatible", "seconds": 0.1})
    step(client, "llm", provider="mistral", key="sk-test", model="")
    assert wizard.read_env()["MISTRAL_API_KEY"] == "sk-test"
    step(client, "notion", **NOTION)
    r = client.post("/setup/experience", headers=H, json={"summary": "Backend engineer, ana@example.com"})
    assert r.status_code == 400 and "contact details" in r.get_json()["error"]
    step(client, "experience", summary="Backend engineer in Python and Go.", level="Mid-level",
         priority_roles=["Backend Engineer"], target_roles=[], strong_stack=["Python", "Go"], nice_stack=[],
         highlights=["Payments service"], experience_md="## Main projects\n### Payments\n")
    step(client, "answer_bank", answers={"first_name": "Ana", "email": "ana@example.com", "phone": ""},
         approve=["first_name", "email", "phone"])
    from jobradar.local.db import open_local

    rows = {r["answer_id"]: r for r in open_local().bank_rows()}
    assert rows["email"]["status"] == "Approved" and rows["phone"]["status"] == "Draft"    # empty stays Draft
    assert "ana@example.com" not in (Path(os.environ["JR_USER_DIR"]) / "profile.yaml").read_text(encoding="utf-8")
    assert "jobalerts-noreply@linkedin.com" in client.get("/setup/optional_keys").data.decode()
    step(client, "optional_keys", keys={"JOOBLE_PT_API_KEY": "jk"}, notify_email=False,
         alert_senders=["LinkedIn <jobalerts-noreply@linkedin.com>", "alerts@example.com", "alerts@example.com"])
    from jobradar import config

    assert config.user_sources()["alert_senders"] == ["jobalerts-noreply@linkedin.com", "alerts@example.com"]
    res = step(client, "config_repo", name="cfg")["result"]
    assert {"MISTRAL_API_KEY", "JOOBLE_PT_API_KEY", "NOTION_TOKEN", "NOTION_JOBS_DB"} <= set(res["secrets"])
    assert client.post("/setup/extension", headers=H, json={}).status_code == 400     # no ping yet
    assert Path(json.loads(json.dumps(str(wizard.install_extension())))).joinpath("manifest.json").is_file()
    client.post("/ext/seen", headers=H)
    step(client, "extension")
    _night()
    step(client, "first_run")
    for k in ("bank",):
        open_local().upsert_answer(f"x{k}", question="q", kind="fact")
    html = client.get("/setup/summary").data.decode()
    assert "All set" in html, html


def test_upgrade_reopens_the_apply_steps(wiz):
    from jobradar import config, wizard

    client, _ = wiz
    step(client, "welcome", mode="radar")
    for s in ("github", "llm", "notion", "basics", "experience", "roles"):
        wizard.mark(s)
    r = client.get("/setup/?upgrade=1")
    assert config.mode() == "apply" and config.module_on("bank") and config.module_on("app")
    assert r.headers["Location"].endswith("/setup/experience")                     # reopened: Apply asks for more
    assert "answer_bank" in [s.id for s in wizard.steps_for("apply")]
    assert "answer_bank" not in [s.id for s in wizard.steps_for("radar")]


def test_cron_follows_the_time_zone():
    from jobradar.wizard import utc_cron, workflow_with_crons

    assert utc_cron("02:00", "UTC") == "0 2 * * *"
    lisbon = utc_cron("02:30", "Europe/Lisbon")
    assert lisbon in ("30 2 * * *", "30 1 * * *")                     # winter / summer time
    wf = workflow_with_crons(TEMPLATE_WF, "03:00", "UTC")
    assert 'cron: "0 3 * * *"   # jobradar:daily' in wf and 'cron: "0 2 * * *"   # jobradar:ats' in wf
    assert 'cron: "0 5 * * 0"   # jobradar:weekly' in wf


def test_cv_import_drafts_profile_with_sources_and_no_contact_details(tmp_path):
    from fakes import FakeLLM
    from jobradar.llm.client import LLMClient
    from jobradar.wizard import cv_text, draft_profile

    reply = {"facts": [{"field": "summary", "value": "Backend engineer (Python, Go).", "source": "Backend Engineer"},
                       {"field": "strong_stack", "value": "Python", "source": "Python, Go"},
                       {"field": "highlights", "value": "Call me at +351 912 345 678", "source": "phone"}],
             "experience_md": "## Main projects\n"}
    fake = FakeLLM(lambda p: reply)
    llm = LLMClient("onboarding", provider="mistral", transport=fake.transport(), sleep=False, ledger_dir=tmp_path)
    out = draft_profile(cv_text("cv.txt", b"Ana Ferreira\nBackend Engineer\nPython, Go"), llm)
    assert out["fields"]["summary"] == "Backend engineer (Python, Go)." and out["fields"]["strong_stack"] == ["Python"]
    assert out["fields"]["highlights"] == [] and out["sources"]["summary"] == ["Backend Engineer"]
    assert "Ana Ferreira" in fake.payloads[0]["messages"][-1]["content"]           # the CV goes to the model…
    assert "Never" not in out["experience_md"]


def test_cities_autocomplete(wiz):
    client, _ = wiz
    hits = client.get("/setup/api/cities?q=lisb").get_json()
    assert hits[0]["name"] in ("Lisbon", "Lisboa") and hits[0]["country"] == "PT" and hits[0]["lat"]


def test_experience_writes_starters_in_full_only(wiz):
    from jobradar import wizard
    from jobradar.apply import cv
    from jobradar.settings import local_dir

    client, tmp = wiz
    for name in ("cv.yaml", "draft_style.md"):
        (local_dir() / name).unlink(missing_ok=True)
    wizard.write_user_yaml("settings.yaml", {"mode": "lite"})
    assert wizard.save_experience({"strong_stack": "Go\nPostgreSQL"})["starters"] == []
    assert not (local_dir() / "cv.yaml").exists()
    wizard.write_user_yaml("settings.yaml", {"mode": "full"})
    assert sorted(wizard.save_experience({"strong_stack": "Go\nPostgreSQL"})["starters"]) == ["cv.yaml", "draft_style.md"]
    src = cv.load_source(local_dir() / "cv.yaml")
    assert src["skills"]["main"]["items"] == "Go, PostgreSQL"
    assert cv.resolve(src, "default")["name"] == "<Your Name>" and cv.render_html(src, "default")  # renders as is
    (local_dir() / "cv.yaml").write_text("name: Mine\n", encoding="utf-8")
    assert wizard.save_experience({})["starters"] == []                    # never overwritten
    assert (local_dir() / "cv.yaml").read_text(encoding="utf-8") == "name: Mine\n"


def test_chromium_status_route(wiz, monkeypatch):
    from jobradar import wizard

    client, _ = wiz
    monkeypatch.setattr(wizard, "chromium_status", lambda: {"package": False, "browser": False, "fix": "uv tool install"})
    assert client.get("/setup/api/chromium").get_json() == {"ok": True, "package": False, "browser": False,
                                                            "fix": "uv tool install"}
    assert client.post("/setup/api/chromium", headers={"X-JR": "1"}).get_json()["ok"] is False


def test_find_alert_senders_counts_job_like_subjects(monkeypatch):
    import imaplib

    from jobradar import wizard
    from email.message import EmailMessage

    def mail(sender, subject):
        m = EmailMessage()
        m["From"], m["Subject"] = sender, subject
        return m.as_bytes()

    heads = [mail("Jobs <alerts@example.net>", "5 нови обяви за Data Engineer"),
             mail("alerts@example.net", "New jobs for you"), mail("friend@example.com", "Lunch?"),
             mail("Karriere <jobagent@example.org>", "Ihr Jobagent: 3 neue Stellen")]

    class Fake:
        def __init__(self, host, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            pass

        def select(self, folder, readonly=False):
            assert readonly

        def search(self, charset, *criteria):
            return "OK", [b"1 2 3 4"]

        def fetch(self, ids, what):
            assert "PEEK" in what and "HEADER.FIELDS" in what
            return "OK", [(b"x", h) for h in heads] + [b")"]

    monkeypatch.setattr(imaplib, "IMAP4_SSL", Fake)
    out = wizard.find_alert_senders({"JR_IMAP_USER": "u@example.com", "JR_IMAP_PASSWORD": "p"})
    assert out["ok"] and [s["sender"] for s in out["senders"]] == ["alerts@example.net", "jobagent@example.org"]
    assert out["senders"][0]["mails"] == 2


def test_alert_boards_for_the_countries_once_each_and_ticked_ones_become_senders(monkeypatch):
    from jobradar import config, wizard

    monkeypatch.setattr(config, "active_domains", lambda: {"business"})
    def user_sources():
        return {"alert_senders": ["jobalerts-noreply@linkedin.com"]}
    user_sources.cache_clear = lambda: None                               # config's cache reset calls it
    monkeypatch.setattr(config, "user_sources", user_sources)
    boards = wizard.alert_boards(["PL", "DE"])
    names = [b["name"] for b in boards]
    assert "Pracuj.pl" in names and "StepStone Germany" in names and "LinkedIn Jobs" in names
    assert "theprotocol.it" not in names                                  # IT-only board, business-only user
    assert names.count("XING Jobs") + names.count("XING") == 1            # DE and Europe-wide: shown once
    assert sum(n.startswith("Indeed") for n in names) == 2                # Poland and Germany, not again Europe-wide
    assert next(b for b in boards if b["name"] == "LinkedIn Jobs")["added"]
    indeed = next(b for b in boards if b["name"] == "Indeed Poland")
    assert indeed["senders"] == ["indeed.com", "indeedemail.com", "indeedmail.com"] and not indeed["added"]
    written = {}
    monkeypatch.setattr(wizard, "write_user_yaml", lambda name, data, merge=True: written.update(data))
    wizard.save_countries_sources({"countries": ["PL"], "alert_add": [",".join(indeed["senders"]), "wysylka.pracuj.pl"]})
    assert written["alert_senders"] == ["jobalerts-noreply@linkedin.com", "indeed.com", "indeedemail.com",
                                        "indeedmail.com", "wysylka.pracuj.pl"]


def test_alerts_step_lists_the_boards_to_follow_for_the_countries(wiz):
    from jobradar import wizard

    client, _ = wiz
    wizard.save_countries_sources({"countries": ["PL"]})
    html = client.get("/setup/optional_keys").data.decode()
    assert "Job-alert e-mails" in html and "Pracuj.pl" in html and 'value="wysylka.pracuj.pl"' in html
    assert "LinkedIn Jobs" in html and 'id="alert-senders"' in html
    assert "Boards to follow" not in client.get("/setup/countries_sources").data.decode()
    v = client.post("/setup/api/sources", headers={"X-JR": "1"}, json={"countries": ["PL"], "opt_in": []}).get_json()
    assert any(b["name"] == "Pracuj.pl" for b in v["alerts"])


def test_unconfirmed_alert_senders_match_on_the_brand_word():
    from jobradar.wizard import alert_boards, sender_keyword

    assert sender_keyword("stepstone.at") == "stepstone" and sender_keyword("mail.infojobs.net") == "infojobs"
    assert sender_keyword("jobs.ie") == "jobs.ie" and sender_keyword("olx.pl") == "olx.pl"        # short or generic
    assert sender_keyword("kariera.gr") == "kariera.gr"
    assert sender_keyword("jobalerts-noreply@linkedin.com") == "jobalerts-noreply@linkedin.com"
    de = {b["name"]: b["senders"] for b in alert_boards(["DE"])}
    assert de["StepStone Germany"] == ["stepstone"] and de["Indeed Germany"][0] == "indeed.com"


def test_roles_step_groups_business_and_tech_packs(wiz):
    client, _ = wiz
    html = client.get("/setup/roles").data.decode()
    biz, tech = html.index("Business and office roles"), html.index("Tech roles")
    assert biz < html.index('value="marketing-content"') < tech < html.index('value="backend"')
    assert biz < html.index('value="sales-cs"') < tech


def test_positions_become_titles_to_keep_and_titles_to_skip(wiz, tmp_path):
    from fakes import FakeLLM
    from jobradar import config, wizard
    from jobradar.llm.client import LLMClient
    from jobradar.match.engine import MatchEngine

    reply = {"packs": ["marketing-content", "not-a-pack"],
             "positions": [{"position": "SEO specialist", "titles": ["SEO Specialist", "Specjalista ds. SEO", "SEO Manager",
                                                                     "SEO Specialist (m/w/d)"]},
                           {"position": "recruiter", "titles": ["Recruiter", "Tech Sourcer"]}],
             "avoid": ["SEO Developer"]}
    fake = FakeLLM(lambda p: reply)
    llm = LLMClient("onboarding", provider="mistral", transport=fake.transport(), sleep=False, ledger_dir=tmp_path)
    out = wizard.expand_positions("SEO specialist, recruiter", client=llm)
    assert out["packs"] == ["marketing-content"]
    assert out["positions"][0]["titles"] == ["SEO Specialist", "Specjalista ds. SEO", "SEO Manager"]   # (m/w/d) dup gone
    assert "English only" in fake.payloads[0]["messages"][0]["content"]          # empty profile: no other languages
    saved = wizard.save_roles({"packs": ["data"], "positions": "SEO specialist, recruiter",
                               "pos_keep": ["SEO Specialist", "Specjalista ds. SEO", "SEO Manager", "Recruiter"],
                               "pos_all": ["SEO Specialist", "Specjalista ds. SEO", "SEO Manager", "Recruiter", "Tech Sourcer"],
                               "pos_avoid": ["SEO Developer"]})
    assert saved["position_skip"] == ["Tech Sourcer", "SEO Developer"]
    assert config.roles()["position_titles"][-1] == "Recruiter"
    engine = MatchEngine.from_config(config.matching_config())
    kept = lambda t: engine.title_verdict(t).tier != "excluded"  # noqa: E731
    assert kept("SEO Manager (m/w/d)") and kept("Specjalista ds. SEO") and kept("Recruiter")   # manager / not-IT lifted
    assert not kept("Tech Sourcer") and not kept("Senior SEO Developer")
    preview = wizard.roles_preview({"packs": ["data"], "pos_keep": ["SEO Specialist"], "pos_all": ["SEO Specialist"]})
    assert "SEO Specialist" in {x["title"] for x in preview["kept"]}
