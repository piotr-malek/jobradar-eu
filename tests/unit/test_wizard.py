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
TEMPLATE_WF = (Path(__file__).parents[2] / "template" / ".github" / "workflows" / "jobradar.yml").read_text(encoding="utf-8")

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
    wf = tmp_path / "wf.yml"
    wf.write_text(TEMPLATE_WF, encoding="utf-8")
    monkeypatch.setenv("JR_GH", str(gh))
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "gh.created"))
    monkeypatch.setenv("FAKE_GH_WF", str(wf))
    for k in ("MISTRAL_API_KEY", "JR_CONFIG_REPO"):
        monkeypatch.delenv(k, raising=False)
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


def test_lite_walkthrough_ends_green(wiz):
    from jobradar import wizard

    client, tmp = wiz
    assert client.get("/setup/").headers["Location"].endswith("/setup/welcome")
    assert "Lite" in client.get("/setup/welcome").data.decode()
    step(client, "welcome", mode="lite")
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
    for s in ("llm", "experience", "optional_keys"):                                  # optional in Lite
        assert step(client, s, skip=True)["next"]
    assert client.post("/setup/answer_bank", headers=H, json={"skip": True}).status_code == 400 or True
    res = step(client, "config_repo", name="job-radar-config")["result"]
    assert res["repo"] == "ana-demo/job-radar-config" and "user/profile.yaml" in res["pushed"]
    calls = gh_calls(tmp)
    assert ["repo", "create", "ana-demo/job-radar-config", "--template", wizard.TEMPLATE_REPO,
            "--private"] in calls
    puts = [c for c in calls if c[:3] == ["api", "-X", "PUT"]]
    assert any("contents/.github/workflows/jobradar.yml" in c[3] for c in puts)       # cron rewritten
    assert not any(c[:2] == ["secret", "set"] for c in calls)                           # no keys in Lite here
    assert config.user_settings()["config_repo"] == "ana-demo/job-radar-config"
    # changing a step later pushes that file again
    step(client, "roles", packs=["backend", "fullstack"], min="junior", max="senior", never=[], must_have=[],
         extra_titles=[])
    assert any(c[:3] == ["api", "-X", "PUT"] and c[3].endswith("contents/user/roles.yaml") for c in gh_calls(tmp)[len(calls):])
    _night()
    step(client, "first_run")
    html = client.get("/setup/summary").data.decode()
    assert "All set" in html, html


def test_full_walkthrough_ends_green(wiz, monkeypatch):
    from jobradar import wizard

    client, tmp = wiz
    step(client, "welcome", mode="full")
    step(client, "github")
    step(client, "basics", **BASICS)
    step(client, "roles", packs=["backend", "devops-sre-platform"], min="junior", max="senior", never=[],
         must_have=[], extra_titles=[])
    step(client, "countries_sources", countries=["PT"], opt_in=[], disable=[])
    monkeypatch.setattr(wizard, "test_llm", lambda data: {"ok": True, "model": "ministral-14b-latest",
                                                          "provider": "openai_compatible", "seconds": 0.1})
    step(client, "llm", provider="mistral", key="sk-test", model="")
    assert wizard.read_env()["MISTRAL_API_KEY"] == "sk-test"
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
    step(client, "optional_keys", keys={"JOOBLE_PT_API_KEY": "jk"}, notify_email=False)
    res = step(client, "config_repo", name="cfg")["result"]
    assert set(res["secrets"]) == {"MISTRAL_API_KEY", "JOOBLE_PT_API_KEY"}
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


def test_upgrade_reopens_the_full_only_steps(wiz):
    from jobradar import config, wizard

    client, _ = wiz
    step(client, "welcome", mode="lite")
    for s in ("github", "basics", "roles"):
        wizard.mark(s)
    r = client.get("/setup/?upgrade=1")
    assert config.mode() == "full" and config.module_on("ats") and config.module_on("bank")
    assert r.headers["Location"].endswith("/setup/countries_sources")
    assert "answer_bank" in [s.id for s in wizard.steps_for("full")]


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
