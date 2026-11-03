"""Sync with the user's private config repo through the GitHub CLI (`gh`), no git clone needed.

* `state` branch: written only by the overnight workflow. Holds `state.db.gz` (one force-pushed commit per run).
  `pull_state()` downloads it to ~/.job-radar/state.db (the app opens it read-only).
* `picks` branch: written only by the app. Holds `picks.jsonl`, one decision per line, append-only.
  `push_picks()` appends the local events not pushed yet (retried when another push got in first);
  `pull_picks()` imports decisions made on another computer.
The workflow reads `picks.jsonl` at the start of each run, so a skipped job never comes back.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from jobradar import config
from jobradar.settings import get_settings

STATE_BRANCH = "state"
PICKS_BRANCH = "picks"
STATE_FILE = "state.db.gz"
PICKS_FILE = "picks.jsonl"
EVENT_FIELDS = ("job_id", "status", "at", "title", "company", "family", "source", "route", "dedup_keys", "note")


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


def _get_picks(repo: str) -> tuple[str, str | None]:
    """(file text, blob sha) of picks.jsonl; ("", None) when the file or branch doesn't exist yet."""
    try:
        out = gh(["api", f"repos/{repo}/contents/{PICKS_FILE}?ref={PICKS_BRANCH}"])
    except SyncError as exc:
        if "404" in str(exc) or "Not Found" in str(exc):
            return "", None
        raise
    meta = json.loads(out or b"{}")
    content = base64.b64decode(meta.get("content") or "").decode("utf-8") if meta.get("content") else ""
    if not content and meta.get("size"):       # > 1 MB: the JSON has no content, read it raw
        content = gh(["api", "-H", "Accept: application/vnd.github.raw",
                      f"repos/{repo}/contents/{PICKS_FILE}?ref={PICKS_BRANCH}"]).decode("utf-8")
    return content, meta.get("sha")


def ensure_picks_branch(repo: str | None = None) -> bool:
    """Create the `picks` branch (an orphan commit with an empty picks.jsonl) when it is missing."""
    repo = repo or config_repo()
    try:
        gh(["api", f"repos/{repo}/branches/{PICKS_BRANCH}"])
        return False
    except SyncError as exc:
        if "404" not in str(exc) and "Not Found" not in str(exc):
            raise
    blob = json.loads(gh(["api", f"repos/{repo}/git/blobs", "-f", "content=", "-f", "encoding=utf-8"]))
    tree = json.loads(gh(["api", f"repos/{repo}/git/trees", "--input", "-"], input=json.dumps(
        {"tree": [{"path": PICKS_FILE, "mode": "100644", "type": "blob", "sha": blob["sha"]}]}).encode()))
    commit = json.loads(gh(["api", f"repos/{repo}/git/commits", "--input", "-"], input=json.dumps(
        {"message": "Start the picks branch", "tree": tree["sha"], "parents": []}).encode()))
    gh(["api", f"repos/{repo}/git/refs", "-f", f"ref=refs/heads/{PICKS_BRANCH}", "-f", f"sha={commit['sha']}"])
    return True


def event_line(e: dict[str, Any]) -> str:
    d = {k: e.get(k) for k in EVENT_FIELDS}
    if isinstance(d.get("dedup_keys"), str):
        d["dedup_keys"] = json.loads(d["dedup_keys"] or "[]")
    return json.dumps({k: v for k, v in d.items() if v not in (None, "", [])}, ensure_ascii=False)


def push_picks(db: Any, repo: str | None = None, *, retries: int = 4) -> dict[str, Any]:
    """Append unpushed decisions to picks.jsonl (one commit). On a conflict (another computer pushed first) the
    file is read again and the append retried."""
    events = db.unpushed()
    if not events:
        return {"pushed": 0}
    repo = repo or config_repo()
    lines = "".join(event_line(e) + "\n" for e in events)
    for attempt in range(retries):
        text, sha = _get_picks(repo)
        if sha is None:
            ensure_picks_branch(repo)
            text, sha = _get_picks(repo)
        body = {"message": f"{len(events)} decision{'s' if len(events) != 1 else ''}", "branch": PICKS_BRANCH,
                "content": base64.b64encode((text + lines).encode("utf-8")).decode()}
        if sha:
            body["sha"] = sha
        try:
            gh(["api", "-X", "PUT", f"repos/{repo}/contents/{PICKS_FILE}", "--input", "-"],
               input=json.dumps(body).encode())
            db.mark_pushed([e["id"] for e in events])
            return {"pushed": len(events), "attempts": attempt + 1}
        except SyncError as exc:
            if attempt == retries - 1 or not any(s in str(exc) for s in ("409", "sha", "conflict", "422")):
                raise
            time.sleep(1.5 * (attempt + 1))
    return {"pushed": 0}


def pull_picks(db: Any, repo: str | None = None) -> dict[str, Any]:
    """Import decisions made elsewhere (another computer) into local.db."""
    text, _ = _get_picks(repo or config_repo())
    rows = []
    for line in text.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return {"imported": db.import_events(rows), "lines": len(rows)}


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
