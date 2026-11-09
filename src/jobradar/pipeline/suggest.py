"""Filter suggestions: jobs the user marks "Not relevant" in Notion teach the filters, with the user's approval.

Each night (after the Notion statuses come in):
1. `apply_ticked`: suggestions the user ticked on the "Filter suggestions" page go into user/learned.yaml (the
   workflow commits it), so the matcher skips those titles from tonight; unticked ones older than a week are removed.
2. `propose`: when there are new Not relevant jobs, one AI call proposes "skip titles with <phrase>" rules. A rule is
   kept only when it catches at least one of those jobs and none the user picked or applied to, nor a title they
   chose in the wizard. Kept rules are added to the page as unticked to-do items.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import yaml

from jobradar import config
from jobradar.settings import user_dir

log = logging.getLogger(__name__)

PAGE_TITLE = "Job Radar: filter suggestions"
EXPIRE_DAYS = 7
MAX_SUGGESTIONS = 5
POSITIVE = ("picked", "applied", "screening", "interview", "offer")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"rules": {"type": "array", "items": {
        "type": "object",
        "properties": {"phrase": {"type": "string"}, "why": {"type": "string"}},
        "required": ["phrase", "why"], "additionalProperties": False}}},
    "required": ["rules"],
    "additionalProperties": False,
}
SYSTEM = """A job seeker marked some job titles "not relevant". Propose at most 5 short phrases (1-3 words) such that
skipping every job title containing the phrase removes these jobs and nothing the job seeker wants. Use words from the
titles themselves (any language). Never propose a phrase that appears in a title they want. "why": one short sentence.
Return no rule rather than a vague one."""


def _hits(pattern: str, titles: list[str]) -> list[str]:
    rx = re.compile(rf"(?<![a-z0-9])(?:{pattern})(?![a-z0-9])")
    return [t for t in titles if rx.search(config.fold_title(t))]


def _learned_path():
    return user_dir() / "learned.yaml"


def _add_learned(patterns: list[str]) -> list[str]:
    path = _learned_path()
    data = config.read_yaml(path) if path.is_file() else {}
    have = list(data.get("never") or [])
    added = [p for p in patterns if p not in have]
    if added:
        data["never"] = have + added
        path.write_text("# Written by the overnight run from the Notion \"Filter suggestions\" page you ticked.\n"
                        + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        config.clear_caches()
    return added


def _rows(store: Any, where: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return store.query(f"SELECT * FROM filter_suggestions WHERE {where} ORDER BY created_at", params or {},
                       table="filter_suggestions")


def _close(store: Any, sid: str, status: str) -> None:
    store.execute("UPDATE filter_suggestions SET status = :s, closed_at = :at WHERE suggestion_id = :id",
                  {"s": status, "at": datetime.now(timezone.utc), "id": sid})


def apply_ticked(store: Any, client: Any, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    pages = {r["page_id"] for r in _rows(store, "status = 'open' AND page_id IS NOT NULL")}
    states: dict[str, bool] = {}
    for page in pages:
        states.update(client.todo_states(page))
    applied, expired = [], 0
    for r in _rows(store, "status = 'open' AND kind = 'never'"):
        checked = states.get(r["block_id"])
        if checked:
            applied.append(r["value"])
            _close(store, r["suggestion_id"], "applied")
            client.remove_block(r["block_id"])
        elif checked is None:                                    # the user deleted it: not wanted
            _close(store, r["suggestion_id"], "dismissed")
        elif str(r["created_at"]) < (now - timedelta(days=EXPIRE_DAYS)).isoformat():
            _close(store, r["suggestion_id"], "expired")
            client.remove_block(r["block_id"])
            expired += 1
    return {"applied": _add_learned(applied), "expired": expired}


def _page(store: Any, client: Any) -> str:
    row = next(iter(_rows(store, "kind = 'page'")), None)
    if row:
        return row["value"]
    parent = os.environ.get("NOTION_PARENT_PAGE")
    if not parent:
        raise RuntimeError("NOTION_PARENT_PAGE is not set")
    from jobradar.notion.client import paragraph

    page = client.create_child_page(parent, PAGE_TITLE, [paragraph(
        "Tick a rule to skip those job titles from the next night on. Rules come from the jobs you set to “Not "
        "relevant”; unticked ones disappear after a week.")])
    store.append("filter_suggestions", [{"suggestion_id": "page", "kind": "page", "value": page, "status": "open",
                                         "created_at": datetime.now(timezone.utc)}])
    return page


def propose(store: Any, client: Any, llm: Any = None, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    last = max([str(r["created_at"]) for r in _rows(store, "kind = 'never'")] or [""])
    rows = store.query(
        """SELECT DISTINCT s.job_id, s.title FROM v_decisions_current d JOIN shortlist s USING (job_id)
           WHERE d.status = 'skipped' AND d.note = 'not_relevant' AND d.at > :last""", {"last": last})
    jobs = len({r["job_id"] for r in rows})
    bad = list(dict.fromkeys(r["title"] for r in rows if r["title"]))
    if not bad:
        return {"proposed": 0, "not_relevant": 0}
    good = [r["title"] for r in store.query(
        "SELECT DISTINCT s.title FROM v_decisions_current d JOIN shortlist s USING (job_id) WHERE d.status IN "
        f"({', '.join(repr(x) for x in POSITIVE)})") if r["title"]]
    good += list(config.roles().get("position_titles") or [])
    existing = set(config.roles().get("never") or []) | set(config.learned_never()) | {
        r["value"] for r in _rows(store, "kind = 'never' AND status = 'open'")}
    from jobradar.llm.client import LLMClient

    own = llm is None
    llm = llm or LLMClient("screen")
    try:
        res = llm.json("Not relevant: " + "; ".join(bad[:60]) + "\nWanted: " + ("; ".join(good[:60]) or "(none yet)"),
                       SCHEMA, name="filter_rules", system=SYSTEM, max_tokens=800)
    finally:
        if own:
            llm.close()
    rules = []
    for rule in (res.get("rules") or [])[:MAX_SUGGESTIONS]:
        phrase = " ".join(str(rule.get("phrase") or "").split())
        pattern = config.title_pattern(phrase) if phrase else ""
        if not pattern or pattern in existing or not config.safe_regex(pattern):
            continue
        caught = _hits(pattern, bad)
        if not caught or _hits(pattern, good):                  # must remove something, never something wanted
            continue
        existing.add(pattern)
        rules.append((phrase, pattern, str(rule.get("why") or "")[:200], caught[:3]))
    if not rules:
        return {"proposed": 0, "not_relevant": jobs}
    page = _page(store, client)
    texts = [f"Skip job titles with “{phrase}”: {why} (e.g. {'; '.join(ex)})" for phrase, _, why, ex in rules]
    blocks = client.append_todos(page, texts)
    store.append("filter_suggestions", [
        {"suggestion_id": uuid.uuid4().hex[:12], "kind": "never", "value": pattern, "why": why, "examples": ex,
         "status": "open", "block_id": block, "page_id": page, "created_at": now}
        for (phrase, pattern, why, ex), block in zip(rules, blocks)])
    return {"proposed": len(rules), "not_relevant": jobs}
