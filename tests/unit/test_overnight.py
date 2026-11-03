"""Overnight: LLM providers and guardrails, the screen, routes, `jr daily` with picks, weekly stats, picks sync."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from fakes import FakeLLM, screen_reply
from jobradar.apply.route import Route, resolve, route_for_members
from jobradar.apply.screen import screen_jobs
from jobradar.http.client import PoliteClient
from jobradar.llm.client import BudgetExhausted, LLMClient, LLMError

NOW = datetime.now(timezone.utc)


def _llm(reply, tmp_path, **kw) -> tuple[LLMClient, FakeLLM]:
    fake = FakeLLM(reply)
    return LLMClient("screen", provider="mistral", transport=fake.transport(), sleep=False, ledger_dir=tmp_path, **kw), fake


# ------------------------------------------------------------------------ LLM
def test_llm_json_strips_think_and_retries_bad_json_once(tmp_path):
    answers = iter(["not json at all", '<think>hmm {"x": 0}</think> {"ok": true}'])
    c, fake = _llm(lambda p: next(answers), tmp_path)
    assert c.json("q", {"type": "object"}) == {"ok": True}
    assert len(fake.payloads) == 2 and fake.payloads[1]["temperature"] == 0.0
    assert fake.payloads[0]["response_format"]["json_schema"]["strict"] is True


def test_llm_gives_up_after_one_retry_and_never_retries_4xx(tmp_path):
    c, fake = _llm(lambda p: httpx.Response(503), tmp_path)
    with pytest.raises(LLMError):
        c.json("q", {"type": "object"})
    assert len(fake.payloads) == 2
    c, fake = _llm(lambda p: httpx.Response(400, json={"message": "bad"}), tmp_path)
    with pytest.raises(LLMError):
        c.json("q", {"type": "object"})
    assert len(fake.payloads) == 1


def test_llm_budget_persists_and_qwen_no_think(tmp_path, monkeypatch):
    monkeypatch.setenv("JR_LLM_DAILY_BUDGET", "2")
    c, _ = _llm(lambda p: "{}", tmp_path)
    c.json("a", {}), c.json("b", {})
    with pytest.raises(BudgetExhausted):
        c.json("c", {})
    c2, _ = _llm(lambda p: "{}", tmp_path)          # new client, same ledger file
    with pytest.raises(BudgetExhausted):
        c2.json("d", {})
    fake = FakeLLM(lambda p: "{}")
    q = LLMClient("prep", provider="ollama", model="qwen3:8b", transport=fake.transport(), sleep=False,
                  ledger_dir=tmp_path / "o")
    q.json("hello", {})
    assert fake.payloads[0]["messages"][-1]["content"].endswith("/no_think")


# --------------------------------------------------------------------- screen
def _jobs(n):
    return [{"job_id": f"JR-{i:06d}", "title": "Analytics Engineer", "company": f"Co{i}", "location": "Remote",
             "description": "dbt BigQuery " * 1000} for i in range(n)]


def test_screen_batches_clips_and_fails_soft(tmp_path):
    calls = {"n": 0}
    base = screen_reply(4)

    def reply(p):
        calls["n"] += 1
        return "garbage" if calls["n"] in (2, 3) else base(p)   # batch 2 fails twice → unscreened

    c, fake = _llm(reply, tmp_path)
    out = screen_jobs(_jobs(12), c)
    assert len(fake.payloads) == 4                                # 3 batches + 1 retry
    assert len(out) == 7 and "JR-000005" not in out and out["JR-000000"]["fit"] == 4
    assert out["JR-000000"]["instructions"] == ["Start your answer with BANANA"]
    assert all(len(p["messages"][-1]["content"]) < 5 * 3700 + 2000 for p in fake.payloads)
    assert "@" not in fake.payloads[0]["messages"][-1]["content"]   # profile has no contact details


def test_screen_fit_drops_a_point_outside_home(tmp_path):
    c, _ = _llm(screen_reply(5, ("remote", "home", "elsewhere", "unclear", "hybrid")), tmp_path)
    berlin = [{**j, "location": "Berlin", "description": "Remote work possible. " + j["description"]}
              for j in _jobs(5)]
    out = screen_jobs(berlin, c)
    assert [out[f"JR-00000{i}"]["fit"] for i in range(5)] == [5, 5, 4, 5, 5]
    assert [out[f"JR-00000{i}"]["work_setup"] for i in range(5)] == ["remote", "home", "elsewhere", "unclear", ""]
    c, _ = _llm(screen_reply(5, ("elsewhere",)), tmp_path)        # home among the cities: no point off
    out = screen_jobs([{**_jobs(1)[0], "location": "Hybrid · Warszawa; Wrocław; Gdańsk"}], c)["JR-000000"]
    assert (out["fit"], out["work_setup"]) == (5, "home")
    for setup in ("remote", "home", "unclear"):                   # the matcher saw hybrid outside the area
        c, _ = _llm(screen_reply(5, (setup,)), tmp_path)
        out = screen_jobs([{**_jobs(1)[0], "location": "Hybrid · Lisboa"}], c)["JR-000000"]
        assert (out["fit"], out["work_setup"]) == (4, "elsewhere")


def test_screen_stops_on_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("JR_LLM_DAILY_BUDGET", "1")
    c, _ = _llm(screen_reply(3), tmp_path)
    assert len(screen_jobs(_jobs(10), c)) == 5


def test_screen_off_never_calls_the_llm(tmp_path):
    assert screen_jobs(_jobs(3)) == {}                            # no llm.yaml in the test profile: screen off


# ------------------------------------------------------------------ providers
def test_anthropic_provider_uses_a_forced_tool_call(tmp_path, monkeypatch):
    seen = []

    def handle(req):
        body = json.loads(req.content)
        seen.append((req.url.path, req.headers.get("x-api-key"), body))
        return httpx.Response(200, json={"content": [{"type": "tool_use", "name": "screen", "input": {"ok": True}}]})

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    c = LLMClient("screen", provider="anthropic", transport=httpx.MockTransport(handle), sleep=False, ledger_dir=tmp_path)
    assert c.json("q", {"type": "object"}, name="screen", system="sys") == {"ok": True}
    path, key, body = seen[0]
    assert path.endswith("/messages") and key == "sk-test" and body["system"] == "sys"
    assert body["tool_choice"] == {"type": "tool", "name": "screen"} and body["model"].startswith("claude-")


def test_server_without_json_schema_falls_back_to_json_mode(tmp_path):
    def reply(p):
        if p["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": "response_format json_schema not supported"})
        assert "JSON schema" in p["messages"][-1]["content"]
        return '{"ok": true}'

    fake = FakeLLM(reply)
    c = LLMClient("screen", provider="groq", model="m", transport=fake.transport(), sleep=False, ledger_dir=tmp_path)
    c.json_mode = "schema"
    assert c.json("q", {"type": "object"}) == {"ok": True} and c.json_mode == "object"


def test_step_config_from_llm_yaml_and_env(tmp_path, monkeypatch):
    from conftest import use_profile

    from jobradar.llm.client import step_config, step_provider

    user = tmp_path / "user"
    user.mkdir()
    (user / "llm.yaml").write_text("default: {provider: gemini}\nsteps: {draft: {provider: claude}}\n")
    use_profile(monkeypatch, user)
    c = step_config("screen")
    assert (c["provider"], c["preset"], c["api_key_env"]) == ("openai_compatible", "gemini", "GEMINI_API_KEY")
    assert step_provider("draft") == "claude" and step_provider("prep") == "openai_compatible"
    monkeypatch.setenv("JR_SCREEN_LLM_PROVIDER", "off")
    assert step_provider("screen") == "off"


def test_local_models_refuse_to_run_on_actions(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(LLMError, match="not on GitHub Actions"):
        LLMClient("screen", provider="ollama", transport=httpx.MockTransport(lambda r: None), ledger_dir=tmp_path)


def test_cost_estimate_scales_with_the_cap():
    from jobradar.llm.cost import estimate_night

    small, big = estimate_night(50, "ministral-14b-latest"), estimate_night(150, "ministral-14b-latest")
    assert 0 < small["usd_per_night"] < big["usd_per_night"] < 1
    assert estimate_night(150, "unknown-model")["usd_per_month"] > 0


# --------------------------------------------------------------------- routes
def test_route_for_members_preference():
    ats = {"source": "ats_greenhouse", "url": "u", "ats_type": "greenhouse", "ats_slug": "acme", "ats_job_id": "42"}
    jj = {"source": "justjoin", "url": "https://justjoin.it/x"}
    r = route_for_members([jj, ats])
    assert (r.vendor, r.slug, r.job, r.readable, r.effort_min) == ("greenhouse", "acme", "42", True, 2)
    r = route_for_members([jj, {"source": "jooble", "url": "https://www.linkedin.com/jobs/view/data-eng-at-x-123456"},
                           {"source": "x", "apply_url": "https://apply.workable.com/acme/j/ABC123DEF/"}])
    assert (r.vendor, r.job, r.readable) == ("workable", "ABC123DEF", True)
    assert route_for_members([{"source": "s", "url": "https://www.linkedin.com/jobs/view/123"}]).vendor == "linkedin"
    assert route_for_members([{"source": "s", "apply_url": "mailto:jobs@x.com"}]).vendor == "email"
    r = route_for_members([jj])
    assert r.vendor == "unknown" and r.effort_min == 6 and r.via == "justjoin"
    assert Route.from_dict(r.to_dict()) == r


def test_resolve_aggregator_page():
    html = b"""<html><a href="https://www.linkedin.com/jobs/view/1">li</a>
      <a href="https://acme.jobs.personio.de/job/77">personio</a>
      <a href="https://jobs.lever.co/acme/0b6d6c6e-1111-2222-3333-444455556666/apply">apply</a></html>"""

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/")
        if req.url.path.startswith("/private"):
            raise AssertionError("robots ignored")
        return httpx.Response(200, content=html, headers={"content-type": "text/html"})

    with PoliteClient("t", transport=httpx.MockTransport(handler), sleep=False, respect_robots=True) as http:
        r = resolve(Route("unknown", "https://board.test/job/1", via="board"), http)
        assert (r.vendor, r.slug, r.readable, r.via) == ("lever", "acme", True, "board")
        blocked = Route("unknown", "https://board.test/private/2")
        assert resolve(blocked, http) == blocked                 # robots → unchanged, no exception
    with PoliteClient("t", transport=httpx.MockTransport(lambda r: httpx.Response(403)), sleep=False) as http:
        assert resolve(Route("unknown", "https://x.test/1"), http).vendor == "unknown"


# --------------------------------------------------------------- jr daily, weekly
def _posting(i, title="Analytics Engineer"):
    from jobradar.models import Posting

    return Posting(source="arbeitnow", source_job_id=str(i), title=title, company=f"Acme {i}", url=f"https://x/{i}",
                   location_raw="Remote - Europe", workplace="remote",
                   description="We build dbt and BigQuery SQL pipelines with our team. " * 5,
                   posted_at=NOW - timedelta(days=1))


def test_daily_reads_picks_and_never_shows_skipped_jobs(tmp_path, monkeypatch):
    from jobradar.pipeline import daily
    from jobradar.pipeline.run import RunSummary, process_postings
    from jobradar.sources.base import SourceSpec
    from jobradar.store.sqlite import SQLiteStore
    from jobradar.match.engine import MatchEngine

    store = SQLiteStore(tmp_path / "state.db")
    v, s, m, _ = process_postings([_posting(1), _posting(2)], spec=SourceSpec(id="arbeitnow", adapter="arbeitnow"),
                                  engine=MatchEngine.load(), known={}, run_id="r")
    for t, rows in (("posting_versions", v), ("posting_sightings", s), ("matches", m)):
        store.append(t, rows)
    preview = daily.run_daily(store, dry_run=True, skip_fetch=True)["shortlist"]["preview"]
    skipped = next(p for p in preview if p["company"] == "Acme 1")["job_id"]
    picks = tmp_path / "picks.jsonl"
    picks.write_text(json.dumps({"job_id": skipped, "status": "skipped", "at": NOW.isoformat()}) + "\nnot json\n")
    monkeypatch.setattr("jobradar.pipeline.run.run_sources", lambda ids, f, **kw: [RunSummary(i, "success", "r") for i in ids])
    out = daily.run_daily(store, picks=picks, minutes=1)
    assert out["decisions_imported"] == 1 and out["shortlist"]["shortlisted"] == 1
    assert [r["company"] for r in store.read("shortlist")] == ["Acme 2"]
    assert "upkeep" in out and out["sources"]["queued"] == 0


def test_run_sources_stops_starting_sources_after_the_deadline(monkeypatch):
    import time

    from jobradar.pipeline import run as run_mod
    from jobradar.sources.base import SourceSpec

    specs = {f"s{i}": SourceSpec(id=f"s{i}", adapter="a") for i in range(3)}
    monkeypatch.setattr(run_mod, "load_specs", lambda **k: specs)
    monkeypatch.setattr(run_mod, "get_adapter", lambda name: type("A", (), {"__module__": "m"})())
    monkeypatch.setattr(run_mod.MatchEngine, "load", classmethod(lambda cls: None))
    out = run_mod.run_sources(list(specs), None, dry_run=True, deadline=time.monotonic() - 1)
    assert [s.status for s in out] == ["not_started"] * 3


def test_weekly_stats_by_source_and_family(tmp_path):
    from jobradar.pipeline.weekly import weekly_stats, weekly_text
    from jobradar.store.sqlite import SQLiteStore

    store = SQLiteStore(tmp_path / "state.db")
    store.append("shortlist", [{"night_id": "n", "night": "2026-10-20", "job_id": j, "family": f, "sources": [s],
                                "route_vendor": "greenhouse"} for j, f, s in
                               [("JR-1", "data", "arbeitnow"), ("JR-2", "data", "justjoin"), ("JR-3", "gtm", "arbeitnow")]])
    store.import_decisions([
        {"job_id": "JR-1", "status": "applied", "at": "2026-10-21T08:00:00+00:00"},
        {"job_id": "JR-1", "status": "interview", "at": "2026-10-28T08:00:00+00:00"},
        {"job_id": "JR-1", "status": "rejected", "at": "2026-11-01T08:00:00+00:00"},
        {"job_id": "JR-2", "status": "applied", "at": "2026-10-22T08:00:00+00:00"},
        {"job_id": "JR-3", "status": "skipped", "at": "2026-10-22T08:00:00+00:00"}])
    st = weekly_stats(store, now=datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert (st["applied_total"], st["replied_total"], st["interviews_total"], st["skipped_total"]) == (2, 1, 1, 1)
    by_source = {r["name"]: r for r in st["by"]["source"]}
    assert by_source["arbeitnow"]["reply_rate"] == 1.0 and by_source["justjoin"]["reply_rate"] == 0.0
    assert st["weeks"] == {"2026-W43": 2} and "Reply rate by family" in weekly_text(st)


# ------------------------------------------------------------------ picks sync
class FakeGitHubContents:
    """The contents API for one file on the picks branch, through a stand-in for `gh`."""

    def __init__(self, conflict_once=False):
        self.text, self.sha, self.puts, self.conflict_once = "", "s-init", 0, conflict_once   # empty picks.jsonl

    def __call__(self, args, input=None, check=True, timeout=120):
        if args[:2] == ["api", "-X"] and args[2] == "PUT":
            body = json.loads(input)
            if self.conflict_once:
                self.conflict_once = False
                self.text += '{"job_id": "JR-OTHER", "status": "skipped", "at": "2026-01-01"}\n'
                self.sha = "s0"
                from jobradar.local.sync import SyncError

                raise SyncError("HTTP 409: sha does not match")
            assert body.get("sha") == self.sha
            self.text = base64.b64decode(body["content"]).decode()
            self.sha = f"s{self.puts}"
            self.puts += 1
            return b"{}"
        if args[0] == "api" and "contents/picks.jsonl" in args[-1]:
            return json.dumps({"content": base64.b64encode(self.text.encode()).decode(), "sha": self.sha,
                               "size": len(self.text)}).encode()
        raise AssertionError(args)


def test_push_picks_appends_once_and_retries_a_conflict(tmp_path, monkeypatch):
    from jobradar.local import sync
    from jobradar.local.db import LocalDB

    db = LocalDB(tmp_path / "local.db")
    db.record("JR-1", "picked", title="AE", company="Acme", dedup_keys=["fz:acme|ae"])
    db.record("JR-2", "skipped")
    fake = FakeGitHubContents(conflict_once=True)
    monkeypatch.setattr(sync, "gh", fake)
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    assert sync.push_picks(db, "me/cfg")["pushed"] == 2
    lines = [json.loads(x) for x in fake.text.splitlines()]
    assert [x["job_id"] for x in lines] == ["JR-OTHER", "JR-1", "JR-2"]          # the other computer's line kept
    assert lines[1]["dedup_keys"] == ["fz:acme|ae"] and not db.unpushed()
    assert sync.push_picks(db, "me/cfg") == {"pushed": 0}                       # nothing new: no commit
    db2 = LocalDB(tmp_path / "other.db")
    assert sync.pull_picks(db2, "me/cfg")["imported"] == 3 and db2.status("JR-1") == "picked"


def test_pull_state_writes_a_sqlite_file(tmp_path, monkeypatch):
    import gzip

    from jobradar.local import sync
    from jobradar.store.sqlite import SQLiteStore

    db = SQLiteStore(tmp_path / "src.db")
    db.init()
    db.close()
    blob = gzip.compress((tmp_path / "src.db").read_bytes())
    monkeypatch.setattr(sync, "gh", lambda args, **k: blob)
    res = sync.pull_state(tmp_path / "out" / "state.db", "me/cfg")
    assert res["bytes"] > 0 and (tmp_path / "out" / "state.db").read_bytes().startswith(b"SQLite format 3")
    monkeypatch.setattr(sync, "gh", lambda args, **k: b"<html>")
    with pytest.raises(sync.SyncError):
        sync.pull_state(tmp_path / "x.db", "me/cfg")
