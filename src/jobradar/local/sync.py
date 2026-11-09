"""Sync with the user's private config repo through the GitHub CLI (`gh`), no git clone needed.

* `state` branch: written only by the overnight workflow. Holds `state.db.gz` (one force-pushed commit per run).
  `pull_state()` downloads it to ~/.job-radar/state.db (the app opens it read-only).
Decisions (pick, skip, applied...) live in the user's Notion Jobs database: `push_picks()` sets the Status of the
jobs decided in the app, `pull_picks()` brings back statuses changed in Notion. The overnight run reads them too, so
a skipped job never comes back.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from jobradar import config
from jobradar.settings import get_settings

STATE_BRANCH = "state"
STATE_FILE = "state.db.gz"


class SyncError(RuntimeError):
    pass


def gh_path() -> str | None:
    """$JR_GH, else gh on the PATH, else the copy the wizard downloaded into ~/.job-radar/bin."""
    if os.environ.get("JR_GH"):
        return shutil.which(os.environ["JR_GH"])
    found = shutil.which("gh")
    if found:
        return found
    from jobradar.ghcli import local_gh

    exe = local_gh()
    return str(exe) if exe else None


def gh(args: list[str], *, input: bytes | None = None, check: bool = True, timeout: int = 120) -> bytes:
    exe = gh_path()
    if not exe:
        raise SyncError("the GitHub CLI (gh) is not installed: run `jr setup` for help")
    res = subprocess.run([exe, *args], input=input, capture_output=True, timeout=timeout)
    if check and res.returncode != 0:
        raise SyncError(res.stderr.decode("utf-8", "replace").strip()[:500] or f"gh {' '.join(args[:2])} failed")
    return res.stdout


def config_repo() -> str:
    repo = os.environ.get("JR_CONFIG_REPO") or config.user_settings().get("config_repo")
    if not repo:
        raise SyncError("no config repo set (user/settings.yaml: config_repo, or JR_CONFIG_REPO); run `jr setup`")
    return repo


def pull_state(dest: Path | None = None, repo: str | None = None) -> dict[str, Any]:
    """Download the newest state.db from the `state` branch (raw contents API, up to 100 MB)."""
    repo = repo or config_repo()
    dest = dest or get_settings().state_db
    raw = gh(["api", "-H", "Accept: application/vnd.github.raw",
              f"repos/{repo}/contents/{STATE_FILE}?ref={STATE_BRANCH}"], timeout=300)
    if not raw:
        raise SyncError("the state branch has no state.db yet (the first overnight run creates it)")
    data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    if not data.startswith(b"SQLite format 3"):
        raise SyncError("downloaded state.db is not a SQLite file")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return {"bytes": len(data), "path": str(dest)}


def push_notion(db: Any, client: Any = None) -> dict[str, Any]:
    """Set the Notion Status of each job with unpushed decisions (its latest one); a job with no page yet (added
    by hand in the app) gets one."""
    from jobradar.notion.client import DECISION_TO_STATUS, NotionClient

    events = db.unpushed()
    if not events:
        return {"pushed": 0, "to": "notion"}
    latest = {e["job_id"]: e for e in events}
    own = client is None
    client = client or NotionClient()
    try:
        for job_id, e in latest.items():
            values: dict[str, Any] = {"Status": DECISION_TO_STATUS.get(e["status"], "New")}
            if e["status"] == "applied":
                values["Applied on"] = str(e.get("at") or "")[:10]
            if e.get("note"):
                values["Notes"] = e["note"]
            page = client.job_page(job_id)
            if page:
                client.set_values(page["_id"], values)
            else:
                client.create_job({"job_id": job_id, "title": e.get("title") or "", "company": e.get("company") or "",
                                   "family": e.get("family"), "sources": [e["source"]] if e.get("source") else []})
                created = client.job_page(job_id)
                if created:
                    client.set_values(created["_id"], values)
        db.mark_pushed([e["id"] for e in events])
    finally:
        if own:
            client.close()
    return {"pushed": len(events), "jobs": len(latest), "to": "notion"}


def pull_notion(db: Any, client: Any = None, *, days: int = 30) -> dict[str, Any]:
    """Status changes made in Notion (the phone, another computer) → local.db events."""
    from datetime import datetime, timedelta, timezone

    from jobradar.notion.client import NOT_RELEVANT, STATUS_TO_DECISION, NotionClient

    own = client is None
    client = client or NotionClient()
    try:
        pages = client.changed_since(datetime.now(timezone.utc) - timedelta(days=days))
    finally:
        if own:
            client.close()
    rows = [{"job_id": p["Job ID"], "status": STATUS_TO_DECISION[p["Status"]], "at": p.get("_edited"),
             "company": p.get("Company"), "note": "not_relevant" if p.get("Status") == NOT_RELEVANT else None}
            for p in pages if p.get("Job ID") and p.get("Status") in STATUS_TO_DECISION]
    return {"imported": db.import_events(rows), "pages": len(pages), "from": "notion"}


def push_picks(db: Any, repo: str | None = None) -> dict[str, Any]:
    """The app's unpushed decisions → Notion (nothing to do until Notion is connected)."""
    from jobradar.pipeline.notion_sync import configured

    return push_notion(db) if configured() else {"pushed": 0, "off": "Notion is not connected"}


def pull_picks(db: Any, repo: str | None = None) -> dict[str, Any]:
    """Statuses changed in Notion (the phone, another computer) → local.db."""
    from jobradar.pipeline.notion_sync import configured

    return pull_notion(db) if configured() else {"imported": 0, "off": "Notion is not connected"}


def workflow_runs(repo: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
    """The latest overnight runs (status, conclusion, link) for the app's status bar."""
    out = gh(["run", "list", "-R", repo or config_repo(), "-L", str(limit), "--json",
              "databaseId,status,conclusion,createdAt,updatedAt,url,displayTitle,event,workflowName"])
    return json.loads(out or b"[]")


def minutes_used(repo: str | None = None) -> dict[str, Any] | None:
    """Actions minutes used this month by the account (GitHub billing API; needs the `user` scope, else None)."""
    try:
        login = json.loads(gh(["api", "user"]))["login"]
        data = json.loads(gh(["api", f"users/{login}/settings/billing/actions"]))
    except (SyncError, ValueError, KeyError):
        return None
    used = data.get("total_minutes_used")
    included = data.get("included_minutes") or 2000
    return {"used": used, "included": included, "share": round((used or 0) / included, 2) if used is not None else None}
