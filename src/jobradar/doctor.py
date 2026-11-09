"""Health checks shared by `jr doctor` and the wizard's summary page. Each check says what is wrong and which wizard
step fixes it."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from jobradar import config
from jobradar.settings import get_settings, local_dir, user_dir


@dataclass
class Check:
    id: str
    label: str
    ok: bool
    detail: str = ""
    step: str = ""          # wizard step that fixes it
    optional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _profile() -> Check:
    p = config.profile()
    loc = p.get("location") or {}
    missing = [k for k in ("city", "country") if not loc.get(k)]
    if not (user_dir() / "profile.yaml").is_file():
        return Check("profile", "Profile", False, f"no {user_dir() / 'profile.yaml'}", "basics")
    if missing:
        return Check("profile", "Profile", False, f"location is missing {', '.join(missing)}", "basics")
    return Check("profile", "Profile", True, f"{loc.get('city')}, {loc.get('country')}")


def _roles() -> Check:
    try:
        packs = config.active_pack_names()
        from jobradar.match.engine import MatchEngine

        MatchEngine.load()
    except Exception as exc:  # noqa: BLE001
        return Check("roles", "Role packs", False, f"{type(exc).__name__}: {exc}"[:200], "roles")
    if not config.roles().get("packs"):
        return Check("roles", "Role packs", False, "no packs chosen (every pack is on)", "roles")
    return Check("roles", "Role packs", True, ", ".join(packs))


def _sources() -> Check:
    from jobradar.sources.registry import load_specs

    s = get_settings()
    specs = load_specs()
    missing = sorted(sid for sid, spec in specs.items() if spec.secrets and not s.has_secrets(spec.secrets))
    countries = config.user_sources().get("countries") or []
    if not countries:
        return Check("sources", "Countries and sources", False, "no target countries chosen", "countries_sources")
    detail = f"{len(specs) - len(missing)} sources for {', '.join(countries)}"
    if missing:
        detail += f"; {len(missing)} wait for a free key ({', '.join(missing[:4])}{'…' if len(missing) > 4 else ''})"
    return Check("sources", "Countries and sources", True, detail)


def _gh() -> Check:
    from jobradar.local.sync import gh_path

    exe = gh_path()
    if not exe:
        return Check("gh", "GitHub CLI", False, "gh is not installed", "github")
    res = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        return Check("gh", "GitHub CLI", False, "gh is not logged in (gh auth login)", "github")
    return Check("gh", "GitHub CLI", True, "logged in")


def _repo() -> Check:
    from jobradar.local.sync import SyncError, config_repo, gh

    try:
        repo = config_repo()
        gh(["repo", "view", repo, "--json", "name"])
    except SyncError as exc:
        return Check("repo", "Private config repo", False, str(exc)[:200], "config_repo")
    return Check("repo", "Private config repo", True, repo)


def _llm() -> Check:
    from jobradar.llm.client import LLMError, step_config

    need = config.module_on("screen")
    try:
        c = step_config("screen")
    except LLMError as exc:
        return Check("llm", "LLM", False, str(exc), "llm", optional=not need)
    if c["provider"] == "off":
        return Check("llm", "LLM", not need, "none set" + (" (the overnight review needs one)" if need else ""),
                     "llm", optional=not need)
    key_env = c.get("api_key_env")
    if key_env and not get_settings().secret(key_env):
        return Check("llm", "LLM", False, f"{key_env} is not set", "llm", optional=not need)
    if c["provider"] == "claude" or c.get("preset") in ("ollama", "lmstudio"):
        return Check("llm", "LLM", not need, f"{c.get('preset') or c['provider']} can't run on GitHub Actions; "
                     "the overnight screen needs an API provider", "llm", optional=not need)
    return Check("llm", "LLM", True, f"{c.get('preset') or c['provider']} · {c.get('model')}", optional=not need)


def _smtp() -> Check:
    want = bool(config.user_settings().get("notify_email"))
    if not want:
        return Check("smtp", "Morning e-mail", True, "off", "optional_keys", optional=True)
    from jobradar.digest import smtp

    try:
        smtp.verify_config()
    except Exception as exc:  # noqa: BLE001
        return Check("smtp", "Morning e-mail", False, str(exc), "optional_keys", optional=True)
    return Check("smtp", "Morning e-mail", True, "configured", optional=True)


def _state() -> Check:
    path = get_settings().state_db
    if not path.is_file():
        return Check("state", "Overnight results", False, "no state.db yet (first run pending, or `jr state pull`)",
                     "first_run")
    from jobradar.store.sqlite import SQLiteStore

    try:
        store = SQLiteStore(path, read_only=True)
        last = store.last_night_at()
        store.close()
    except Exception as exc:  # noqa: BLE001
        return Check("state", "Overnight results", False, f"{type(exc).__name__}", "first_run")
    if not last:
        return Check("state", "Overnight results", False, "no shortlist yet", "first_run")
    hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    return Check("state", "Overnight results", hours < 36, f"last shortlist {hours:.0f} h ago", "first_run")


def _minutes() -> Check:
    from jobradar.local.sync import minutes_used

    m = minutes_used()
    if not m or m.get("used") is None:
        return Check("minutes", "Actions minutes", True, "unknown (gh needs the `user` scope to read billing)",
                     optional=True)
    share = m["share"] or 0
    return Check("minutes", "Actions minutes", share < 0.9,
                 f"{m['used']} of {m['included']} used this month" + (" (over 75%)" if share >= 0.75 else ""),
                 optional=True)


def _cv() -> Check:
    full = config.mode() == "apply"
    has = (local_dir() / "cv.yaml").is_file()
    return Check("cv", "CV source", has or not full, "~/.job-radar/cv.yaml" if has else "none yet", "experience",
                 optional=not full)


def _playwright() -> Check:
    if config.mode() != "apply":
        return Check("playwright", "PDF renderer", True, "not needed in Radar", optional=True)
    from jobradar.wizard import chromium_status

    st = chromium_status()
    if not st["package"]:
        return Check("playwright", "PDF renderer", False, f"playwright is not installed: {st['fix']}", "experience",
                     optional=True)
    if not st["browser"]:
        return Check("playwright", "PDF renderer", False, "Chromium not downloaded (wizard: Your experience)",
                     "experience", optional=True)
    return Check("playwright", "PDF renderer", True, "Chromium ready", optional=True)


def _extension() -> Check:
    if config.mode() != "apply":
        return Check("extension", "Chrome extension", True, "not used in Radar", optional=True)
    from jobradar.local.db import open_local

    seen = open_local().get_meta("extension_seen")
    return Check("extension", "Chrome extension", bool(seen), f"last seen {seen}" if seen else "not connected yet",
                 "extension", optional=True)


def _bank() -> Check:
    if not config.module_on("bank"):
        return Check("bank", "Answer bank", True, "off", optional=True)
    from jobradar.local.db import open_local

    rows = open_local().bank_rows()
    approved = sum(r["status"] == "Approved" and bool(r["answer"].strip()) for r in rows)
    return Check("bank", "Answer bank", approved >= 5, f"{approved} of {len(rows)} answers approved", "answer_bank",
                 optional=True)


def _notion() -> Check:
    from jobradar.pipeline.notion_sync import configured

    if not configured():
        return Check("notion", "Notion", not config.module_on("notion"), "not connected", "notion",
                     optional=not config.module_on("notion"))
    return Check("notion", "Notion", True, "Jobs database connected")


def _notion_live() -> Check:
    from jobradar.notion.client import NotionClient, NotionError
    from jobradar.pipeline.notion_sync import configured

    if not configured():
        return Check("notion_live", "Notion reachable", True, "not connected", optional=True)
    try:
        with NotionClient() as client:
            client.request("GET", f"/data_sources/{client.need_jobs()}")
    except (NotionError, OSError) as exc:
        return Check("notion_live", "Notion reachable", False, f"{exc}"[:200], "notion")
    return Check("notion_live", "Notion reachable", True, "the integration can write to the Jobs database")


CHECKS: list[Callable[[], Check]] = [_profile, _roles, _sources, _gh, _repo, _llm, _notion, _notion_live, _smtp, _cv,
                                     _bank, _playwright, _extension, _state, _minutes]
OFFLINE = {"_gh", "_repo", "_minutes", "_state", "_notion_live"}


def run_checks(*, offline: bool = False) -> list[Check]:
    out = []
    for fn in CHECKS:
        if offline and fn.__name__ in OFFLINE:
            continue
        try:
            out.append(fn())
        except Exception as exc:  # noqa: BLE001 - one broken check never hides the others
            out.append(Check(fn.__name__.strip("_"), fn.__name__.strip("_"), False, f"{type(exc).__name__}: {exc}"[:200]))
    return out


def all_green(checks: list[Check]) -> bool:
    return all(c.ok for c in checks if not c.optional)


def gh_installed() -> bool:
    return shutil.which("gh") is not None
