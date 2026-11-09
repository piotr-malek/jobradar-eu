"""Notion as the job list: database setup, nightly page writes in priority order within a time budget, review
updates on later nights, and the user's Status changes read back into `decisions`."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from jobradar.notion.client import JOBS_SCHEMA, NotionClient, NotionError, page_id_from
from jobradar.pipeline import notion_sync
from jobradar.store.sqlite import SQLiteStore

NOW = datetime.now(timezone.utc)


class FakeNotion:
    """Just enough of the Notion API (2026-03-11): databases with one data source, pages, queries."""

    def __init__(self):
        self.props: dict[str, dict] = {}
        self.pages: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.throttle_once = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        path, method = req.url.path.removeprefix("/v1"), req.method
        self.calls.append((method, path))
        body = json.loads(req.content) if req.content else {}
        if self.throttle_once:
            self.throttle_once = False
            return httpx.Response(429, headers={"Retry-After": "0"})
        if method == "POST" and path == "/databases":
            self.props = dict(body["initial_data_source"]["properties"])
            return httpx.Response(200, json={"id": "db1", "url": "https://notion.so/db1",
                                             "data_sources": [{"id": "ds1"}]})
        if path == "/data_sources/ds1" and method == "GET":
            return httpx.Response(200, json={"properties": self.props})
        if path == "/data_sources/ds1" and method == "PATCH":
            self.props.update(body["properties"])
            return httpx.Response(200, json={})
        if method == "POST" and path == "/pages":
            pid = uuid.uuid4().hex
            self.pages[pid] = {"id": pid, "url": f"https://notion.so/{pid}", "properties": body["properties"],
                               "children": body.get("children") or [], "in_trash": False,
                               "last_edited_time": NOW.isoformat()}
            return httpx.Response(200, json=self.pages[pid])
        if method == "PATCH" and path.startswith("/pages/"):
            page = self.pages.get(path.split("/")[-1])
            if page is None:
                return httpx.Response(404, json={"code": "object_not_found", "message": "no page"})
            page["properties"].update(body.get("properties") or {})
            page["in_trash"] = body.get("in_trash", page["in_trash"])
            return httpx.Response(200, json=page)
        if method == "POST" and path == "/data_sources/ds1/query":
            hits = [p for p in self.pages.values() if not p["in_trash"] and self._match(p, body.get("filter"))]
            return httpx.Response(200, json={"results": hits, "has_more": False})
        return httpx.Response(400, json={"code": "unexpected", "message": f"{method} {path}"})

    def _match(self, page: dict, flt: dict | None) -> bool:
        if not flt:
            return True
        if "and" in flt:
            return all(self._match(page, f) for f in flt["and"])
        if flt.get("timestamp") == "last_edited_time":
            return page["last_edited_time"] >= flt["last_edited_time"]["on_or_after"]
        prop = page["properties"].get(flt["property"]) or {}
        if "select" in flt:
            name = (prop.get("select") or {}).get("name")
            return name != flt["select"]["does_not_equal"] if "does_not_equal" in flt["select"] \
                else name == flt["select"]["equals"]
        text = "".join(x["text"]["content"] for x in prop.get("rich_text") or [])
        return text == flt["rich_text"]["equals"]

    def value(self, pid: str, name: str):
        p = self.pages[pid]["properties"][name]
        kind = next(iter(p))
        v = p[kind]
        if kind in ("title", "rich_text"):
            return "".join(x["text"]["content"] for x in v)
        return (v or {}).get("name") if kind == "select" else v

    def by_job(self) -> dict[str, str]:
        return {self.value(pid, "Job ID"): pid for pid in self.pages}

    def user_sets(self, pid: str, status: str) -> None:
        self.pages[pid]["properties"]["Status"] = {"select": {"name": status}}
        self.pages[pid]["last_edited_time"] = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()

    def page_view(self, page: dict) -> dict:     # what the API returns: typed properties
        out = {}
        for name, p in page["properties"].items():
            kind = next(iter(p))
            if kind in ("title", "rich_text"):
                out[name] = {"type": kind, kind: [{"plain_text": x["text"]["content"]} for x in p[kind]]}
            else:
                out[name] = {"type": kind, kind: p[kind]}
        return {**page, "properties": out}


@pytest.fixture()
def fake(monkeypatch):
    f = FakeNotion()
    original = f.handle
    f.handle = lambda req: _typed(f, original(req))
    monkeypatch.setenv("NOTION_TOKEN", "secret_test")
    monkeypatch.setenv("NOTION_JOBS_DB", "ds1")
    monkeypatch.setattr(notion_sync, "NotionClient",
                        lambda *a, **k: NotionClient(transport=f.transport(), sleep=False))
    return f


def _typed(f: FakeNotion, resp: httpx.Response) -> httpx.Response:
    """Query results come back with typed properties, like the real API."""
    if resp.status_code == 200 and resp.content:
        data = resp.json()
        if "results" in data:
            data["results"] = [f.page_view(p) for p in data["results"]]
            return httpx.Response(200, json=data)
    return resp


def _store(tmp_path, n_screened=3, n_open=2):
    store = SQLiteStore(tmp_path / "state.db")
    rows = []
    for i in range(n_screened + n_open):
        screened = i < n_screened
        fit = [5, 3, 2, 4, 4][i] if screened else None
        rows.append({"night_id": "n1", "night": NOW.date(), "job_id": f"JR-{i}", "title": f"Role {i}",
                     "company": f"Co {i}", "location_display": "Remote", "rank": i, "section": "remote",
                     "family": "data", "sources": ["arbeitnow"], "url": f"https://x/{i}", "posting_url": f"https://x/{i}",
                     "route_url": f"https://x/{i}/apply", "description": f"Posting text {i}.\nSecond paragraph.",
                     "copies": json.dumps([{"title": f"Role {i}", "company": f"Co {i}", "source": "jooble",
                                            "url": f"https://j/{i}"}]),
                     "fit": fit, "summary": "Data team" if screened else None, "watch_out": ["German C1"] if screened else [],
                     "instructions": [], "screened_at": NOW if screened else None,
                     "screen_model": "gemini:gemini-3.5-flash-lite" if screened else None,
                     "hidden": "low_fit" if fit is not None and fit <= 2 else None})
    store.append("shortlist", rows)
    return store


def test_init_creates_the_jobs_database_and_adds_new_properties(fake):
    c = NotionClient(transport=fake.transport(), sleep=False, jobs_ds="")
    out = c.init_jobs("https://www.notion.so/My-job-search-0123456789abcdef0123456789abcdef")
    assert out["data_source"] == "ds1" and set(fake.props) == set(JOBS_SCHEMA)
    assert [o["name"] for o in fake.props["Status"]["select"]["options"]][:4] == ["New", "Apply", "Later", "Skip"]
    del fake.props["Deadline"]
    assert c.ensure_jobs_schema() == ["Deadline"] and c.ensure_jobs_schema() == []
    assert page_id_from("0123456789abcdef0123456789abcdef") == "0123456789abcdef0123456789abcdef"
    with pytest.raises(NotionError):
        page_id_from("https://www.notion.so/no-id-here")


def test_push_writes_best_reviewed_jobs_first_and_never_low_fit(fake, tmp_path):
    store = _store(tmp_path)
    fake.throttle_once = True                                     # a 429 is retried after Retry-After
    out = notion_sync.run_push(store)
    assert (out["created"], out["left"], out["failed"]) == (4, 0, 0)
    created = [fake.value(pid, "Job ID") for pid in fake.pages]
    assert created == ["JR-0", "JR-1", "JR-3", "JR-4"]               # fit 5, fit 3, then unreviewed newest first
    pid = fake.by_job()["JR-0"]
    assert fake.value(pid, "Fit") == 5 and fake.value(pid, "Status") == "New"
    assert fake.value(pid, "Reviewed by") == "gemini:gemini-3.5-flash-lite"
    assert fake.value(pid, "Watch-outs") == "German C1" and fake.value(pid, "Section") == "Remote"
    body = json.dumps(fake.pages[pid]["children"])
    assert "Posting text 0." in body and "Also listed (1)" in body
    assert not fake.value(fake.by_job()["JR-3"], "Fit")
    again = notion_sync.run_push(store)
    assert again["created"] == 0 and len(fake.pages) == 4          # each job once


def test_push_stops_at_the_deadline_and_continues_the_next_night(fake, tmp_path):
    store = _store(tmp_path)
    assert notion_sync.run_push(store, deadline=time.monotonic() - 1)["left"] == 4
    assert not fake.pages
    assert notion_sync.run_push(store)["created"] == 4


def test_a_later_review_updates_the_page_and_a_low_fit_trashes_it_while_new(fake, tmp_path):
    store = _store(tmp_path)
    notion_sync.run_push(store)
    later = datetime.now(timezone.utc)                            # reviewed tonight, after the page was written
    for job, fit, hidden in (("JR-3", 4, None), ("JR-4", 1, "low_fit")):
        store.execute("UPDATE shortlist SET fit = :fit, summary = 'Reviewed', hidden = :hidden, screened_at = :at, "
                      "screen_model = 'mistral:m', notion_synced_at = NULL WHERE job_id = :job", {"fit": fit, "hidden": hidden, "at": later,
                                                                        "job": job})
    out = notion_sync.run_push(store)
    assert (out["updated"], out["trashed"], out["created"]) == (1, 1, 0)
    pids = fake.by_job()
    assert fake.value(pids["JR-3"], "Fit") == 4 and fake.value(pids["JR-3"], "Summary") == "Reviewed"
    assert fake.pages[pids["JR-4"]]["in_trash"]
    assert notion_sync.run_push(store)["updated"] == 0             # nothing new: no writes


def test_status_changes_in_notion_become_decisions(fake, tmp_path):
    from jobradar.pipeline.shortlist import screen_queue

    store = _store(tmp_path)
    notion_sync.run_push(store)
    pids = fake.by_job()
    fake.user_sets(pids["JR-0"], "Apply")
    fake.user_sets(pids["JR-1"], "Not relevant")
    fake.user_sets(pids["JR-3"], "Skip")
    assert notion_sync.run_pull(store) == {"decisions_imported": 3}
    current = store.decisions_current()
    assert current["JR-0"]["status"] == "picked" and current["JR-3"]["status"] == "skipped"
    assert current["JR-1"]["status"] == "skipped" and current["JR-1"]["note"] == "not_relevant"
    assert notion_sync.run_pull(store) == {"decisions_imported": 0}   # read again: no duplicates

    class NoLLM:                                                       # the skipped job is never sent to the LLM
        def __getattr__(self, name):
            raise AssertionError("no LLM call expected")
    seen = []
    import jobradar.apply.screen as screen_mod
    orig = screen_mod.screen_chain
    screen_mod.screen_chain = lambda jobs, **k: seen.extend(j["job_id"] for j in jobs) or {}
    try:
        screen_queue(store, llm=NoLLM(), now=NOW)
    finally:
        screen_mod.screen_chain = orig
    assert seen == ["JR-4"]


def test_daily_run_pulls_before_and_pushes_after_the_shortlist(fake, tmp_path, monkeypatch):
    from jobradar.pipeline import daily

    store = _store(tmp_path)
    calls = []
    monkeypatch.setattr(notion_sync, "run_pull", lambda s: calls.append("pull") or {"decisions_imported": 0})
    monkeypatch.setattr("jobradar.pipeline.shortlist.build_shortlist",
                        lambda *a, **k: calls.append("shortlist") or {"status": "done"})
    out = daily.run_daily(store, skip_fetch=True, minutes=1)
    assert calls == ["pull", "shortlist"] and out["notion"]["created"] == 4


def test_without_notion_settings_nothing_is_called(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    store = _store(tmp_path)
    assert notion_sync.run_push(store) == {"status": "off"} and notion_sync.run_pull(store) == {"status": "off"}


def test_weekly_reads_notion_then_posts_the_week_next_to_the_database(fake, tmp_path, monkeypatch):
    from jobradar.pipeline.weekly import run_weekly

    monkeypatch.setenv("NOTION_PARENT_PAGE", "0123456789abcdef0123456789abcdef")
    store = _store(tmp_path)
    notion_sync.run_push(store)
    fake.user_sets(fake.by_job()["JR-0"], "Applied")
    posted = []
    orig = fake.handle
    fake.handle = lambda req: (posted.append(json.loads(req.content)) if req.url.path == "/v1/pages" and
                               json.loads(req.content)["parent"].get("page_id") else None) or orig(req)
    stats = run_weekly(store, email=False)
    assert stats["notion_pull"] == {"decisions_imported": 1} and stats["applied_total"] == 1
    assert stats["notion_page"]["status"] == "posted"
    assert "Applied 1" in json.dumps(posted[0]["children"])


def test_app_decisions_go_to_notion_and_notion_changes_come_back(fake, tmp_path, monkeypatch):
    from jobradar.local import sync
    from jobradar.local.db import LocalDB

    store = _store(tmp_path)
    notion_sync.run_push(store)
    monkeypatch.setattr("jobradar.notion.client.NotionClient.__init__",
                        _patched_init(fake, NotionClient.__init__))
    db = LocalDB(tmp_path / "local.db")
    db.record("JR-0", "picked", title="Role 0", company="Co 0")
    db.record("JR-0", "applied", title="Role 0", company="Co 0")
    db.record("M-1", "applied", title="Hand-added role", company="Corner Shop")
    out = sync.push_picks(db)
    assert out == {"pushed": 3, "jobs": 2, "to": "notion"} and not db.unpushed()
    pids = fake.by_job()
    assert fake.value(pids["JR-0"], "Status") == "Applied" and fake.value(pids["M-1"], "Status") == "Applied"
    assert fake.value(pids["JR-0"], "Applied on")["start"] == NOW.date().isoformat()
    fake.user_sets(pids["JR-1"], "Interview")                       # moved on the phone
    assert sync.pull_picks(db)["imported"] >= 1
    assert any(r["job_id"] == "JR-1" and r["status"] == "interview"
               for r in db.conn.execute("SELECT job_id, status FROM events"))


def _patched_init(fake, init):
    def patched(self, token=None, *, jobs_ds=None, transport=None, sleep=True):
        init(self, token, jobs_ds=jobs_ds, transport=transport or fake.transport(), sleep=False)
    return patched


def test_a_job_reviewed_on_a_later_night_gets_its_page_updated(fake, tmp_path):
    from fakes import FakeLLM, screen_reply
    from jobradar.llm.client import LLMClient
    from jobradar.pipeline.shortlist import screen_queue

    store = _store(tmp_path)
    notion_sync.run_push(store)                                     # JR-3 and JR-4 written without a review
    llm = LLMClient("screen", provider="mistral", transport=FakeLLM(screen_reply(4)).transport(), sleep=False,
                    ledger_dir=tmp_path)
    assert screen_queue(store, llm=llm, now=NOW)["screened"] == 2   # same clock tick as the push is fine
    out = notion_sync.run_push(store)
    assert out["updated"] == 2 and fake.value(fake.by_job()["JR-3"], "Fit") == 4
    assert notion_sync.run_push(store)["updated"] == 0
