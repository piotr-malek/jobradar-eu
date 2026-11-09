"""Setup wizard logic (the Flask pages are in app/setup.py). Every step saves only once its check passes; progress is
kept in ~/.job-radar/setup_state.json so the user can close the browser and continue later.

What goes where:
  user/ (pushed to the private config repo): settings.yaml, profile.yaml (no contact details), roles.yaml,
         sources.yaml, llm.yaml, packs/
  ~/.job-radar (never pushed): .env (keys), local.db (answer bank, decisions), experience.md, cv.yaml,
         draft_style.md, state.db
"""

from __future__ import annotations

import json
import os
import platform
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from jobradar import config
from jobradar.settings import PACKAGE_DIR, local_dir, reset_caches, user_dir

TEMPLATE_REPO = os.environ.get("JR_TEMPLATE_REPO", "piotr-malek/jobradar-eu-config-template")
WORKFLOW = ".github/workflows/jobradar.yml"
USER_FILES = ("settings.yaml", "profile.yaml", "roles.yaml", "sources.yaml", "llm.yaml", "matching.yaml",
              "companies.yaml")
LOCAL_ONLY = (".env", "local.db", "experience.md", "cv.yaml", "draft_style.md", "state.db", "out/", "cache/")
WORK_SETUPS = ("remote_eu", "remote_country", "hybrid", "onsite")
LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2", "native")
LEVEL_LABELS = {"A1": "A1 · beginner", "A2": "A2 · basic", "B1": "B1 · intermediate", "B2": "B2 · good working level",
                "C1": "C1 · fluent", "C2": "C2 · near native", "native": "Native"}


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    modes: tuple[str, ...]
    optional_in: tuple[str, ...] = ()      # modes where the step can be skipped


STEPS = [
    Step("welcome", "Welcome", ("lite", "full")),
    Step("github", "GitHub", ("lite", "full")),
    Step("basics", "Where and how you work", ("lite", "full")),
    Step("roles", "Roles", ("lite", "full")),
    Step("countries_sources", "Countries and job boards", ("lite", "full")),
    Step("llm", "AI model", ("lite", "full"), optional_in=("lite",)),
    Step("experience", "Your experience", ("lite", "full"), optional_in=("lite",)),
    Step("answer_bank", "Answer bank", ("full",)),
    Step("optional_keys", "Free extras", ("lite", "full"), optional_in=("lite", "full")),
    Step("config_repo", "Your private repo", ("lite", "full")),
    Step("extension", "Chrome extension", ("full",), optional_in=("full",)),
    Step("first_run", "First run", ("lite", "full")),
    Step("summary", "All set", ("lite", "full")),
]
STEP_IDS = [s.id for s in STEPS]
FULL_ONLY = ("answer_bank", "extension")


# ------------------------------------------------------------------ state
def state_path() -> Path:
    return local_dir() / "setup_state.json"


def load_state() -> dict[str, Any]:
    try:
        st = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    st.setdefault("mode", config.user_settings().get("mode") or "lite")
    st.setdefault("done", {})
    st.setdefault("skipped", [])
    return st


def save_state(st: dict[str, Any]) -> None:
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps(st, indent=1), encoding="utf-8")


def steps_for(mode: str) -> list[Step]:
    return [s for s in STEPS if mode in s.modes]


def mark(step_id: str, *, skipped: bool = False) -> dict[str, Any]:
    st = load_state()
    if skipped:
        if step_id not in st["skipped"]:
            st["skipped"].append(step_id)
        st["done"].pop(step_id, None)
    else:
        st["done"][step_id] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        st["skipped"] = [s for s in st["skipped"] if s != step_id]
    save_state(st)
    return st


def next_step(st: dict[str, Any] | None = None) -> str:
    st = st or load_state()
    for s in steps_for(st["mode"]):
        if s.id not in st["done"] and s.id not in st["skipped"]:
            return s.id
    return "summary"


# ------------------------------------------------------------------ files
def write_user_yaml(name: str, data: dict[str, Any], *, merge: bool = True) -> Path:
    """Write user/<name> (merging into what is there) and push it to the config repo when that exists."""
    path = user_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    cur = config.read_yaml(path) if merge else {}
    out = config.deep_merge(cur, data) if merge else data
    for k, v in data.items():                 # lists replace, they don't merge
        if isinstance(v, list):
            out[k] = v
    path.write_text(yaml.safe_dump(out, allow_unicode=True, sort_keys=False), encoding="utf-8")
    reset_caches()
    _push_later(name)
    return path


def _push_later(name: str) -> None:
    st = load_state()
    if st.get("repo"):
        pending = set(st.get("pending_push") or [])
        pending.add(name)
        st["pending_push"] = sorted(pending)
        save_state(st)


def env_path() -> Path:
    return local_dir() / ".env"


def read_env() -> dict[str, str]:
    out = {}
    if env_path().is_file():
        for line in env_path().read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


def set_env(updates: dict[str, str | None]) -> Path:
    """Update ~/.job-radar/.env (mode 600) and this process's environment; None removes a key."""
    cur = read_env()
    for k, v in updates.items():
        if v is None or v == "":
            cur.pop(k, None)
            os.environ.pop(k, None)
        else:
            cur[k] = v.strip()
            os.environ[k] = v.strip()
    path = env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Job Radar keys (local only; GitHub gets them as Actions secrets)\n"
                    + "".join(f"{k}={v}\n" for k, v in sorted(cur.items())), encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    reset_caches()
    return path


# ------------------------------------------------------------------ steps: save
def save_welcome(data: dict[str, Any]) -> dict[str, Any]:
    mode = data.get("mode") if data.get("mode") in ("lite", "full") else "lite"
    st = load_state()
    st["mode"] = mode
    save_state(st)
    write_user_yaml("settings.yaml", {"mode": mode})
    return {"mode": mode}


def save_basics(data: dict[str, Any]) -> dict[str, Any]:
    from jobradar.normalize.location import city_point

    city = (data.get("city") or "").strip()
    country = (data.get("country") or "").strip().upper()[:2]
    if not city or not country:
        raise ValueError("home city and country are required")
    hit = city_point(city)
    lat, lon = data.get("lat"), data.get("lon")
    if (lat in (None, "") or lon in (None, "")) and hit and hit[0] == country:
        lat, lon = hit[1], hit[2]
    setups = [s for s in data.get("work_setups") or [] if s in WORK_SETUPS] or ["remote_eu", "remote_country"]
    langs = {config.lang_code(k): v for k, v in (data.get("languages") or {}).items() if k and v}
    if not langs:
        raise ValueError("add at least one language you work in")
    radius = data.get("hybrid_radius_km")
    loc = {"city": city, "country": country, "lat": float(lat) if lat not in (None, "") else None,
           "lon": float(lon) if lon not in (None, "") else None,
           "hybrid_radius_km": int(radius) if str(radius or "").strip().isdigit() else 30,
           "work_setups": setups, "relocate_to": [c.upper() for c in data.get("relocate_to") or []]}
    if data.get("work_countries"):
        loc["work_countries"] = [c.upper() for c in data["work_countries"]]
    write_user_yaml("profile.yaml", {"location": loc, "languages": langs,
                                     "contracts": data.get("contracts") or ["employment"]})
    tz = data.get("timezone") or "UTC"
    try:
        ZoneInfo(tz)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"unknown time zone {tz!r}") from exc
    write_user_yaml("settings.yaml", {"timezone": tz, "run_at": data.get("run_at") or "02:00"})
    if data.get("salary"):                    # a negotiating fact: kept in the local answer bank, never pushed
        from jobradar.local.db import open_local

        db = open_local()
        db.seed_bank()
        db.upsert_answer("salary_expectation", answer=str(data["salary"]), status="Draft")
    return {"location": loc, "languages": langs}


def save_roles(data: dict[str, Any]) -> dict[str, Any]:
    packs = [p for p in data.get("packs") or [] if p in {**config.available_packs(), **config.user_packs()}]
    if not packs:
        raise ValueError("pick at least one role pack")
    bad = [w for w in (data.get("never") or []) + (data.get("must_have") or []) + (data.get("extra_titles") or [])
           if not config.safe_regex(w)]
    if bad:
        raise ValueError(f"not a valid pattern: {', '.join(bad[:3])}")
    out = {"packs": packs,
           "seniority": {"min": data.get("min") or "junior", "max": data.get("max") or "senior"},
           "never": [w for w in data.get("never") or [] if w.strip()],
           "must_have": [w for w in data.get("must_have") or [] if w.strip()],
           "extra_titles": [w for w in data.get("extra_titles") or [] if w.strip()]}
    write_user_yaml("roles.yaml", out, merge=False)
    return out


def roles_preview(data: dict[str, Any], titles: list[str] | None = None) -> dict[str, Any]:
    """Sample titles marked kept or dropped under the roles being chosen (nothing saved)."""
    from jobradar.match.engine import MatchEngine

    packs = [p for p in data.get("packs") or [] if p]
    if not packs:
        return {"kept": [], "dropped": []}
    r = {"packs": packs, "seniority": {"min": data.get("min") or "junior", "max": data.get("max") or "senior"},
         "never": [w for w in data.get("never") or [] if config.safe_regex(w)],
         "extra_titles": [w for w in data.get("extra_titles") or [] if config.safe_regex(w)]}
    engine = MatchEngine.from_config(config.matching_config(r=r, packs=packs))
    sample = titles or sample_titles()
    kept, dropped = [], []
    for t in sample:
        v = engine.title_verdict(t)
        (dropped if v.tier == "excluded" else kept).append({"title": t, "why": v.drop_reason or v.family})
    return {"kept": kept, "dropped": dropped}


def sample_titles(n: int = 400) -> list[str]:
    """Real-looking titles for the preview: every pack's golden titles plus a few seniority variants."""
    out: list[str] = []
    for p in config.available_packs().values():
        g = p.get("golden") or {}
        out += list(g.get("keep", []))[:20] + list(g.get("drop", []))[:5]
    out += ["Senior Backend Engineer", "Lead Data Engineer", "Junior Frontend Developer", "Staff Software Engineer",
            "Head of Engineering", "Engineering Manager", "Software Engineering Intern"]
    return list(dict.fromkeys(out))[:n]


def country_names() -> dict[str, str]:
    """ISO code → English country name (GeoNames countryInfo)."""
    from jobradar.normalize.location import country_names as names

    return dict(names())


def country_options() -> list[dict[str, Any]]:
    """The countries the wizard offers (EU, EEA, Switzerland), by name, with how many local boards run there without
    a key and how many company career pages in the registry were seen hiring there."""
    from jobradar.normalize.location import ELIGIBLE
    from jobradar.pipeline.ats import shipped_registry
    from jobradar.sources.registry import load_specs

    names = country_names()
    local: dict[str, int] = {}
    for spec in load_specs(all_sources=True).values():
        if spec.enabled and not spec.opt_in and not spec.secrets:
            for c in spec.countries:
                local[c.upper()] = local.get(c.upper(), 0) + 1
    companies: dict[str, int] = {}
    for row in shipped_registry()[1]:
        for cc in {x.partition(":")[0] for x in (row.get("countries") or "").split()}:
            companies[cc] = companies.get(cc, 0) + 1
    return sorted(({"code": c, "name": names.get(c, c), "boards": local.get(c, 0), "companies": companies.get(c, 0)}
                   for c in ELIGIBLE), key=lambda r: r["name"])


def countries_sources_view(countries: list[str], opt_in: list[str] | None = None,
                           disabled: list[str] | None = None) -> dict[str, Any]:
    """The boards for these countries, split into the ones that will run for this user and the ones that won't
    (with the reason), using the same rules as the overnight run (registry.selected)."""
    from jobradar.settings import get_settings
    from jobradar.sources.registry import PAN_EU, load_specs, selected

    s = get_settings()
    want = [c.upper() for c in countries]
    user = {"countries": want, "opt_in": list(opt_in or [])}
    names = country_names()
    on, off = [], []
    for sid, spec in load_specs(all_sources=True).items():
        covers = {c.upper() for c in spec.countries}
        if not (covers & PAN_EU or covers & set(want)):
            continue
        if not spec.enabled and spec.opt_in is None and sid != "email_alerts":
            continue                                  # switched off for everyone (e.g. paid or fragile)
        row = {"id": sid, "label": spec.label or sid,
               "covers": "all of Europe / remote" if covers & PAN_EU else ", ".join(names.get(c, c) for c in sorted(covers)),
               "needs_key": bool(spec.secrets) and not s.has_secrets(spec.secrets), "local_only": spec.runner == "local",
               "checked": sid not in (disabled or [])}
        if sid == "email_alerts":
            row["note"] = "needs a mailbox: add it in Free extras"
        elif row["needs_key"]:
            row["note"] = "needs a free key: add it in Free extras"
        elif row["local_only"]:
            row["note"] = "blocks GitHub: only searched when you run Job Radar on this computer"
        if selected(spec, user) or sid == "email_alerts":
            on.append(row)
            continue
        if spec.opt_in == "purpose":
            row["why"] = "only with “UN, EU institutions and NGOs” switched on"
        elif spec.opt_in == "polish_only":
            row["why"] = "only with “Polish-language boards” switched on"
        elif spec.packs:
            packs = config.available_packs()
            row["why"] = "only for " + " or ".join(f"“{(packs.get(k) or {}).get('label') or k}”" for k in spec.packs) + " roles"
        else:
            row["why"] = "off by default"
        off.append(row)
    on.sort(key=lambda r: (r["covers"] == "all of Europe / remote", r["needs_key"] or r["id"] == "email_alerts", r["label"].lower()))
    off.sort(key=lambda r: r["label"].lower())
    ready = sum(1 for r in on if r["checked"] and not r["needs_key"] and r["id"] != "email_alerts")
    return {"on": on, "off": off, "ready": ready, "alerts": alert_boards(want)}


ROLE_LABELS = {"general": "all kinds of jobs", "it": "IT jobs", "business": "office and business jobs"}


def alert_boards(countries: list[str]) -> list[dict[str, Any]]:
    """Big boards in these countries (and Europe-wide) that can't be searched overnight, so their job-alert e-mails
    are the way in (defaults/alert_boards.yaml). `added`: its sender is already in the user's alert senders."""
    from jobradar.settings import PACKAGE_DIR

    data = config.read_yaml(PACKAGE_DIR / "alert_boards.yaml").get("countries") or {}
    names = country_names()
    have = " ".join(s.lower() for s in config.user_sources().get("alert_senders") or [])
    out = []
    for cc in [*[c.upper() for c in countries], "EU"]:
        for b in data.get(cc) or []:
            sender = str(b.get("sender") or "").strip().lower()
            out.append({"id": b.get("id"), "name": b.get("name"), "country": "Europe-wide" if cc == "EU" else names.get(cc, cc),
                        "url": b.get("alerts_url") or b.get("url"), "sender": sender,
                        "roles": ROLE_LABELS.get(b.get("roles") or "general", b.get("roles")),
                        "account": bool(b.get("account_needed")), "added": bool(sender) and sender in have})
    return out


def save_countries_sources(data: dict[str, Any]) -> dict[str, Any]:
    countries = [c.upper() for c in data.get("countries") or [] if c]
    if not countries:
        raise ValueError("pick at least one country")
    out = {"countries": countries, "opt_in": [o for o in data.get("opt_in") or [] if o in ("polish_only", "purpose")],
           "disable": list(data.get("disable") or []), "enable": list(data.get("enable") or [])}
    senders = list(config.user_sources().get("alert_senders") or [])   # set on the e-mail card: kept
    for sender in data.get("alert_add") or []:
        if sender and sender.strip().lower() not in (x.lower() for x in senders):
            senders.append(sender.strip().lower())
    if senders:
        out["alert_senders"] = senders
    write_user_yaml("sources.yaml", out, merge=False)
    return out


LLM_PRESETS = [   # shown first to last: free tiers first
    {"id": "mistral", "label": "Mistral (free tier)", "signup": "https://console.mistral.ai/api-keys",
     "env": "MISTRAL_API_KEY", "actions": True},
    {"id": "gemini", "label": "Google Gemini (free tier)", "signup": "https://aistudio.google.com/apikey",
     "env": "GEMINI_API_KEY", "actions": True},
    {"id": "groq", "label": "Groq (free tier)", "signup": "https://console.groq.com/keys", "env": "GROQ_API_KEY",
     "actions": True},
    {"id": "openrouter", "label": "OpenRouter (free models)", "signup": "https://openrouter.ai/settings/keys",
     "env": "OPENROUTER_API_KEY", "actions": True},
    {"id": "openai", "label": "OpenAI", "signup": "https://platform.openai.com/api-keys", "env": "OPENAI_API_KEY",
     "actions": True},
    {"id": "anthropic", "label": "Anthropic Claude", "signup": "https://console.anthropic.com/settings/keys",
     "env": "ANTHROPIC_API_KEY", "actions": True},
    {"id": "ollama", "label": "Ollama (on this computer: drafts only)", "signup": "https://ollama.com/download",
     "env": None, "actions": False},
    {"id": "claude", "label": "Claude Code CLI (on this computer: drafts only)", "signup": "https://claude.com/claude-code",
     "env": None, "actions": False},
]


def llm_settings(provider: str, model: str | None = None, base_url: str | None = None) -> dict[str, Any]:
    from jobradar.llm.client import step_config_for

    c = step_config_for(provider)
    if model:
        c["model"] = model
    if base_url:
        c["base_url"] = base_url
    return c


def test_llm(data: dict[str, Any]) -> dict[str, Any]:
    from jobradar.llm.client import LLMError, test_call

    preset = next((p for p in LLM_PRESETS if p["id"] == data.get("provider")), None)
    if preset is None:
        return {"ok": False, "error": "pick a provider"}
    if preset["env"] and data.get("key"):
        os.environ[preset["env"]] = data["key"].strip()
    try:
        return test_call("screen", settings=llm_settings(preset["id"], data.get("model"), data.get("base_url")))
    except LLMError as exc:
        return {"ok": False, "error": str(exc)[:300]}


def save_llm(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("provider") in (None, "", "off"):
        write_user_yaml("llm.yaml", {"default": {"provider": "off"}}, merge=False)
        return {"provider": "off"}
    preset = next((p for p in LLM_PRESETS if p["id"] == data["provider"]), None)
    if preset is None:
        raise ValueError("unknown provider")
    res = test_llm(data)
    if not res.get("ok"):
        raise ValueError(f"the test call failed: {res.get('error')}")
    if preset["env"]:
        set_env({preset["env"]: data.get("key") or os.environ.get(preset["env"])})
    entry = {"provider": preset["id"], **({"model": data["model"]} if data.get("model") else {})}
    if data.get("base_url"):
        entry["base_url"] = data["base_url"]
    out: dict[str, Any] = {"default": entry}
    if not preset["actions"]:                  # local models can't run the overnight screen
        out["steps"] = {"screen": {"provider": "off"}}
    if data.get("draft_provider"):
        out.setdefault("steps", {})["draft"] = {"provider": data["draft_provider"],
                                                **({"model": data["draft_model"]} if data.get("draft_model") else {})}
    write_user_yaml("llm.yaml", out, merge=False)
    if config.mode() == "full" and not preset["actions"]:
        return {**out, "warning": "Full mode screens jobs overnight on GitHub: that needs an API provider"}
    return out


# ------------------------------------------------------------------ experience (CV import)
def cv_text(filename: str, blob: bytes) -> str:
    """Text of an uploaded CV (PDF, DOCX or plain text)."""
    name = filename.lower()
    if name.endswith(".pdf"):
        import io

        from pypdf import PdfReader

        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(blob)).pages)
    if name.endswith(".docx"):
        import io

        import docx

        d = docx.Document(io.BytesIO(blob))
        return "\n".join(p.text for p in d.paragraphs)
    return blob.decode("utf-8", "replace")


PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"facts": {"type": "array", "items": {
        "type": "object",
        "properties": {"field": {"type": "string", "enum": ["summary", "level", "priority_roles", "target_roles",
                                                            "strong_stack", "nice_stack", "highlights"]},
                       "value": {"type": "string"}, "source": {"type": "string"}},
        "required": ["field", "value", "source"], "additionalProperties": False}},
        "experience_md": {"type": "string"}},
    "required": ["facts", "experience_md"], "additionalProperties": False,
}
PROFILE_SYSTEM = """You turn a CV into a job-search profile. Use only facts written in the CV; never invent.
For each fact return field, value and source (the CV line it came from, quoted verbatim, max 160 characters).
- summary: 1-2 sentences, role + core stack + what the person builds (no adjectives, no contact details)
- level: one sentence with the seniority and years in the main role
- priority_roles / target_roles: one entry per role title (priority = what the CV is clearly about)
- strong_stack / nice_stack: one entry per technology
- highlights: up to 5 entries, each a concrete achievement or project with numbers when the CV has them
experience_md: a markdown list of the person's projects in three tiers ("## Main projects", "## Supporting projects",
"## Small fixes"), each project one short paragraph: what was built, for whom, with which tools, the result.
No names, e-mails, phone numbers, addresses or links anywhere."""
LIST_FIELDS = ("priority_roles", "target_roles", "strong_stack", "nice_stack", "highlights")


def draft_profile(text: str, client: Any = None) -> dict[str, Any]:
    """LLM draft of the profile fields, each with the CV line it came from (the wizard shows them side by side)."""
    from jobradar.llm.client import LLMClient

    own = client is None
    client = client or LLMClient("onboarding")
    try:
        out = client.json(f"CV:\n{text[:20000]}", PROFILE_SCHEMA, name="profile", system=PROFILE_SYSTEM,
                          max_tokens=3000)
    finally:
        if own:
            client.close()
    fields: dict[str, Any] = {f: [] for f in LIST_FIELDS}
    sources: dict[str, list[str]] = {}
    for f in out.get("facts") or []:
        k, v = f.get("field"), (f.get("value") or "").strip()
        if not k or not v or _looks_like_contact(v):
            continue
        if k in LIST_FIELDS:
            fields[k].append(v)
        else:
            fields[k] = v
        sources.setdefault(k, []).append(f.get("source") or "")
    return {"fields": fields, "sources": sources, "experience_md": out.get("experience_md") or ""}


_CONTACT = re.compile(r"@|\+?\d[\d\s().-]{7,}\d|linkedin\.com|github\.com/", re.I)


def _looks_like_contact(v: str) -> bool:
    return bool(_CONTACT.search(v))


def save_experience(data: dict[str, Any]) -> dict[str, Any]:
    """Profile fields → user/profile.yaml (no contact details); experience.md / draft_style.md → ~/.job-radar."""
    prof: dict[str, Any] = {}
    for k in ("summary", "level"):
        if data.get(k):
            prof[k] = " ".join(str(data[k]).split())
    for k in LIST_FIELDS:
        v = data.get(k)
        if isinstance(v, str):
            v = [x.strip() for x in v.splitlines() if x.strip()]
        if v:
            prof[k] = v
    leaked = [k for k, v in prof.items() if _looks_like_contact(json.dumps(v))]
    if leaked:
        raise ValueError(f"remove contact details from: {', '.join(leaked)} (they stay on this computer, in the bank)")
    if prof:
        write_user_yaml("profile.yaml", prof)
    local = local_dir()
    local.mkdir(parents=True, exist_ok=True)
    if data.get("experience_md"):
        (local / "experience.md").write_text(data["experience_md"], encoding="utf-8")
    if data.get("draft_style"):
        (local / "draft_style.md").write_text(data["draft_style"], encoding="utf-8")
    if data.get("cv_yaml"):
        yaml.safe_load(data["cv_yaml"])                     # must parse
        (local / "cv.yaml").write_text(data["cv_yaml"], encoding="utf-8")
    return {"profile_fields": sorted(prof), "starters": ensure_local_files() if config.mode() == "full" else []}


CV_STARTER = """# Your CV source (stays on this computer). `jr cv` renders one PDF per variant into ~/.job-radar/out/cv.
# Replace every <...>. Ids (the keys under skills, bullets, projects) are short names you pick; variants refer to them.
# A bullet can be a string, or {default: "...", <variant>: "..."} when one variant needs other wording of the same fact.
# The example in the Job Radar repository (examples/lisbon-backend/home/cv.yaml) shows a filled-in one.
name: <Your Name>
file_name: <Your_Name>_CV.pdf
contact: ["<City, Country>", "<email>", "<linkedin.com/in/...>"]
skills:
  main: {label: <Main skills>, items: "{skills}"}
experience:
  - company: <Company>
    place: <City>
    roles:
      - id: job1
        title: <Job title>
        dates: <2023 – now>
        bullets:
          b1: <What you built or changed, with a number if you have one.>
          b2: <Another result.>
projects:
  p1: {name: "<Project>", text: "<One line about it.>"}
education:
  - {school: "<School>", degree: "<Degree>", dates: "<2017 – 2021>"}
variants:
  default:
    headline: <Your role · main skills>
    summary: <One or two sentences about what you do best.>
    skills: [main]
    projects: [p1]
"""


def ensure_local_files() -> list[str]:
    """Starters that drafts and PDFs need, written only when missing: draft_style.md (the shipped style guide) and
    cv.yaml (a skeleton with the profile's strong stack filled in). Returns the names written."""
    local = local_dir()
    local.mkdir(parents=True, exist_ok=True)
    written = []
    style = local / "draft_style.md"
    if not style.exists():
        style.write_text((PACKAGE_DIR / "draft_style.md").read_text(encoding="utf-8"), encoding="utf-8")
        written.append("draft_style.md")
    cv = local / "cv.yaml"
    if not cv.exists():
        stack = ", ".join(config.profile().get("strong_stack") or []) or "<Python, SQL, ...>"
        cv.write_text(CV_STARTER.replace("{skills}", stack.replace('"', "'")), encoding="utf-8")
        written.append("cv.yaml")
    return written


def chromium_status() -> dict[str, Any]:
    """Is the PDF renderer ready? package: the playwright package imports; browser: its Chromium is downloaded."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"package": False, "browser": False,
                "fix": "uv tool install --reinstall 'jobradar-eu[full]'   (or: pipx inject jobradar-eu playwright)"}
    try:
        with sync_playwright() as pw:
            ok = Path(pw.chromium.executable_path).exists()
    except Exception:  # noqa: BLE001
        ok = False
    return {"package": True, "browser": ok}


def install_chromium() -> dict[str, Any]:
    """Download Playwright's Chromium (~150 MB, a few minutes) for the CV and cover-letter PDFs."""
    st = chromium_status()
    if not st["package"] or st["browser"]:
        return st
    r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], capture_output=True, text=True)
    out = chromium_status()
    if r.returncode != 0:
        out["error"] = (r.stderr or r.stdout).strip()[-500:]
    return out


# ------------------------------------------------------------------ answer bank
def save_bank(data: dict[str, Any]) -> dict[str, Any]:
    """Answers typed in the wizard: approved ones go straight to Approved, the rest stay Draft."""
    from jobradar.local.db import open_local

    db = open_local()
    db.seed_bank()
    approve = set(data.get("approve") or [])
    n = 0
    for aid, answer in (data.get("answers") or {}).items():
        if answer is None:
            continue
        db.upsert_answer(aid, answer=str(answer), status="Approved" if aid in approve and str(answer).strip()
                         else "Draft")
        n += 1
    return {"saved": n, "approved": len(approve)}


# ------------------------------------------------------------------ optional keys
KEY_CARDS = [
    {"id": "jooble", "label": "Jooble", "signup": "https://jooble.org/api/about", "per_country": True,
     "env": "JOOBLE_{CC}_API_KEY",
     "what": "A big job search engine. One free key per country site: ask for it on their API page."},
    {"id": "adzuna", "label": "Adzuna", "signup": "https://developer.adzuna.com/signup", "modes": ["full"],
     "env": ["ADZUNA_APP_ID", "ADZUNA_APP_KEY"], "countries": ["AT", "BE", "DE", "ES", "FR", "IT", "NL", "PL", "CH"],
     "what": "A job search engine that collects jobs from many boards. Free key: sign up, then copy the app ID and key."},
    {"id": "theirstack", "label": "TheirStack (free plan)", "signup": "https://app.theirstack.com/settings/api-key",
     "env": "THEIR_STACK_API_KEY", "what": "Jobs collected from company career pages; about 40 a week on the free plan."},
    {"id": "itjobs_pt", "label": "ITJobs.pt", "signup": "https://www.itjobs.pt/api", "env": "ITJOBS_PT_API_KEY",
     "countries": ["PT"], "what": "The main IT job board in Portugal."},
    {"id": "imap", "label": "Job-alert e-mails (for example LinkedIn alerts)",
     "signup": "https://myaccount.google.com/apppasswords", "env": ["JR_IMAP_USER", "JR_IMAP_PASSWORD"],
     "what": "Reads the job alerts that LinkedIn and job boards e-mail you, so their jobs show up here too."},
    {"id": "smtp", "label": "A morning e-mail", "signup": "https://myaccount.google.com/apppasswords",
     "env": ["SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"], "what": "A short “12 new jobs are ready” e-mail each morning."},
]
KEY_LABELS = {"JR_IMAP_USER": "Your e-mail address", "JR_IMAP_PASSWORD": "App password (not your normal password)",
              "SMTP_USER": "Gmail address that sends it", "SMTP_PASSWORD": "App password (not your normal password)",
              "EMAIL_TO": "Send it to (e-mail address)"}


def key_cards(countries: list[str], mode: str | None = None) -> list[dict[str, Any]]:
    """The Free extras cards that apply to these countries and this setup mode (Jooble only for its country sites
    the user picked)."""
    from jobradar.sources.registry import load_specs

    mode = mode or config.mode()

    want = {c.upper() for c in countries}
    sites = {c["id"]: [s.countries[0] for s in load_specs(all_sources=True).values() if s.adapter == c["id"]]
             for c in KEY_CARDS if c.get("per_country")}
    out = []
    for c in KEY_CARDS:
        if c.get("modes") and mode not in c["modes"]:
            continue
        if c.get("per_country"):
            cc = [x for x in sites[c["id"]] if x in want]
            if cc:
                out.append({**c, "countries": cc})
        elif not c.get("countries") or want & set(c["countries"]):
            out.append(c)
    return out


def test_key(card: str, values: dict[str, str]) -> dict[str, Any]:
    """One live check per card: a keyed API page, an IMAP login, or a test e-mail."""
    try:
        if card == "smtp":
            from jobradar.digest import smtp
            from jobradar.settings import Settings, get_settings

            base = get_settings()
            s = Settings(**{**base.__dict__, "smtp_user": values.get("SMTP_USER"),
                            "smtp_password": values.get("SMTP_PASSWORD"), "email_to": values.get("EMAIL_TO")})
            smtp.send("Job Radar test e-mail", "<p>It works: the morning e-mail will come from here.</p>",
                      "It works: the morning e-mail will come from here.", settings=s)
            return {"ok": True, "detail": "test e-mail sent"}
        if card == "imap":
            import imaplib

            with imaplib.IMAP4_SSL(values.get("JR_IMAP_HOST") or "imap.gmail.com", timeout=30) as m:
                m.login(values["JR_IMAP_USER"], values["JR_IMAP_PASSWORD"])
            return {"ok": True, "detail": "IMAP login ok"}
        import httpx

        if card == "theirstack":
            r = httpx.post("https://api.theirstack.com/v1/jobs/search", timeout=30,
                           headers={"Authorization": f"Bearer {values['THEIR_STACK_API_KEY']}"},
                           json={"limit": 1, "posted_at_max_age_days": 3, "job_country_code_or": ["DE"]})
            return {"ok": r.status_code == 200, "detail": f"HTTP {r.status_code}"}
        if card == "adzuna":
            r = httpx.get("https://api.adzuna.com/v1/api/jobs/de/search/1", timeout=30,
                          params={"app_id": values["ADZUNA_APP_ID"], "app_key": values["ADZUNA_APP_KEY"],
                                  "results_per_page": 1})
            return {"ok": r.status_code == 200, "detail": f"HTTP {r.status_code}"}
        if card == "itjobs_pt":
            r = httpx.get("https://api.itjobs.pt/job/list.json", params={"api_key": values["ITJOBS_PT_API_KEY"],
                                                                         "limit": 1}, timeout=30)
            return {"ok": r.status_code == 200, "detail": f"HTTP {r.status_code}"}
        if card == "jooble":
            (env, key), = [(k, v) for k, v in values.items() if v]
            cc = env.split("_")[1].lower()
            r = httpx.post(f"https://{cc}.jooble.org/api/{key}", json={"keywords": "engineer", "page": "1"},
                           timeout=30)
            return {"ok": r.status_code == 200, "detail": f"HTTP {r.status_code}"}
    except Exception as exc:  # noqa: BLE001 - shown on the card
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200]}
    return {"ok": False, "detail": "unknown card"}


_JOB_SUBJECT = re.compile(
    r"\bjobs?\b|job alert|vacanc|vacature|stellen|jobagent|praca|ofert|emploi|offres?\b|lavoro|offerte|empleo|"
    r"trabajo|vagas?\b|emprego|työ|jobb|tjenest|работ|обяв|állás|práce|pracovn|posao|poslov|delo\b|munc|"
    r"θέσ|εργασ|darbo|darba|töö|career|hiring|positions?\b|roles?\b|opening", re.I)


def find_alert_senders(values: dict[str, str] | None = None, days: int = 30, scan: int = 800) -> dict[str, Any]:
    """Senders in the mailbox whose recent subjects look like job alerts, most mails first (headers only, read-only)."""
    import email as email_lib
    import imaplib
    from datetime import datetime, timedelta, timezone
    from email.header import decode_header, make_header
    from email.utils import parseaddr

    env = {**read_env(), **{k: v for k, v in (values or {}).items() if v}}
    if not (env.get("JR_IMAP_USER") and env.get("JR_IMAP_PASSWORD")):
        return {"ok": False, "detail": "enter the e-mail address and app password first", "senders": []}
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
    counts: dict[str, dict[str, Any]] = {}
    try:
        with imaplib.IMAP4_SSL(env.get("JR_IMAP_HOST") or "imap.gmail.com", timeout=60) as m:
            m.login(env["JR_IMAP_USER"], env["JR_IMAP_PASSWORD"])
            m.select('"INBOX"', readonly=True)
            _, data = m.search(None, "SINCE", since)
            ids = (data[0] or b"").split()[-scan:]
            if ids:
                _, parts = m.fetch(b",".join(ids), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
                for part in parts:
                    if not isinstance(part, tuple):
                        continue
                    msg = email_lib.message_from_bytes(part[1])
                    addr = parseaddr(msg.get("From") or "")[1].lower()
                    subject = str(make_header(decode_header(msg.get("Subject") or "")))
                    if addr and _JOB_SUBJECT.search(subject):
                        c = counts.setdefault(addr, {"sender": addr, "mails": 0, "subject": subject[:90]})
                        c["mails"] += 1
    except Exception as exc:  # noqa: BLE001 - shown on the card
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200], "senders": []}
    listed = {s.lower() for s in config.user_sources().get("alert_senders") or []}
    found = sorted((c for c in counts.values() if c["sender"] not in listed), key=lambda c: -c["mails"])[:25]
    return {"ok": True, "detail": f"{len(found)} possible job-alert senders in the last {days} days", "senders": found}


def save_keys(data: dict[str, Any]) -> dict[str, Any]:
    keys = {k: v for k, v in (data.get("keys") or {}).items() if re.fullmatch(r"[A-Z0-9_]+", k)}
    set_env(keys)
    if "alert_senders" in data:
        from email.utils import parseaddr

        senders = [parseaddr(s)[1] or s.strip() for s in data.get("alert_senders") or [] if s.strip()]
        write_user_yaml("sources.yaml", {"alert_senders": list(dict.fromkeys(senders))})
    if "notify_email" in data:
        write_user_yaml("settings.yaml", {"notify_email": bool(data["notify_email"])})
    return {"saved": sorted(k for k, v in keys.items() if v)}


# ------------------------------------------------------------------ GitHub
def gh(args: list[str], *, input: bytes | None = None, check: bool = True) -> str:
    from jobradar.local.sync import gh as _gh

    return _gh(args, input=input, check=check).decode("utf-8", "replace")


def install_help() -> dict[str, str]:
    system = platform.system()
    cmds = {"Darwin": "brew install gh", "Windows": "winget install --id GitHub.cli",
            "Linux": "see https://github.com/cli/cli/blob/trunk/docs/install_linux.md (apt, dnf, …)"}
    return {"os": system, "command": cmds.get(system, cmds["Linux"]), "url": "https://cli.github.com/"}


def github_status() -> dict[str, Any]:
    from jobradar.local.sync import gh_path

    exe = gh_path()
    out: dict[str, Any] = {"installed": bool(exe), "help": install_help()}
    if not exe:
        return out
    res = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=30)
    text = res.stdout + res.stderr
    out["logged_in"] = res.returncode == 0
    m = re.search(r"Token scopes:\s*(.+)", text)
    scopes = {s.strip(" '\"") for s in m.group(1).split(",")} if m else set()
    out["scopes"] = sorted(scopes)
    out["missing_scopes"] = [s for s in ("repo", "workflow") if s not in scopes] if scopes else []
    acct = re.search(r"account (\S+)", text) or re.search(r"Logged in to \S+ as (\S+)", text)
    out["login"] = acct.group(1) if acct else None
    out["ok"] = out["logged_in"] and not out["missing_scopes"]
    out["fix"] = ("gh auth login --scopes repo,workflow" if not out["logged_in"]
                  else f"gh auth refresh --scopes {','.join(out['missing_scopes'])}" if out["missing_scopes"] else "")
    return out


def utc_cron(run_at: str, tz: str, *, minus_hours: int = 0, weekday: str | None = None) -> str:
    """'02:00' Europe/Lisbon → '0 2 * * *' in UTC (today's offset; summer time moves it by an hour)."""
    h, m = (int(x) for x in (run_at or "02:00").split(":")[:2])
    local = datetime.now(ZoneInfo(tz)).replace(hour=h, minute=m, second=0, microsecond=0) - timedelta(hours=minus_hours)
    u = local.astimezone(timezone.utc)
    return f"{u.minute} {u.hour} * * {weekday if weekday is not None else '*'}"


def workflow_with_crons(text: str, run_at: str, tz: str) -> str:
    crons = {"daily": utc_cron(run_at, tz), "ats": utc_cron(run_at, tz, minus_hours=1),
             "weekly": utc_cron(run_at, tz, minus_hours=-2, weekday="0")}
    for tag, cron in crons.items():
        text = re.sub(rf'(cron: )"[^"]*"(\s*# jobradar:{tag}\b)', rf'\1"{cron}"\2', text)
    return text


def workflow_with_package(text: str, version: str | None = None, spec: str | None = None) -> str:
    """Pin the overnight run to the Job Radar this computer runs: its version, or $JOBRADAR_SPEC (e.g. a git URL)
    when the installer used one."""
    from jobradar import __version__

    version = version or __version__
    spec = os.environ.get("JOBRADAR_SPEC", "") if spec is None else spec
    spec = spec.replace("[full]", "")                     # the overnight run needs no browser
    text = re.sub(r'(JOBRADAR_VERSION: )"[^"]*"', rf'\1"{version}"', text)
    return re.sub(r'(JOBRADAR_SPEC: )"[^"]*"', lambda m: m.group(1) + json.dumps(spec), text)


def secrets_to_set() -> dict[str, str]:
    """Keys from ~/.job-radar/.env that the overnight run needs (LLM keys, source keys, IMAP/SMTP)."""
    from jobradar.settings import SECRET_NAMES

    env = read_env()
    return {k: v for k, v in env.items() if v and SECRET_NAMES.fullmatch(k)}


def _put_file(repo: str, path: str, content: bytes, message: str, branch: str | None = None) -> None:
    import base64

    sha = None
    try:
        meta = json.loads(gh(["api", f"repos/{repo}/contents/{path}" + (f"?ref={branch}" if branch else "")]))
        sha = meta.get("sha")
    except Exception:  # noqa: BLE001 - a new file
        pass
    body: dict[str, Any] = {"message": message, "content": base64.b64encode(content).decode()}
    if sha:
        body["sha"] = sha
    if branch:
        body["branch"] = branch
    gh(["api", "-X", "PUT", f"repos/{repo}/contents/{path}", "--input", "-"], input=json.dumps(body).encode())


def user_files() -> list[Path]:
    d = user_dir()
    files = [d / n for n in USER_FILES if (d / n).is_file()]
    if (d / "packs").is_dir():
        files += sorted((d / "packs").glob("*.yaml"))
    return files


def push_user_files(repo: str, names: list[str] | None = None) -> list[str]:
    pushed = []
    for path in user_files():
        rel = path.relative_to(user_dir()).as_posix()
        if names is not None and rel not in names:
            continue
        _put_file(repo, f"user/{rel}", path.read_bytes(), f"Update user/{rel}")
        pushed.append(f"user/{rel}")
    return pushed


def flush_pending() -> list[str]:
    """Push user files changed since the config repo was created (called after each saved step)."""
    st = load_state()
    if not st.get("repo") or not st.get("pending_push"):
        return []
    pushed = push_user_files(st["repo"], st["pending_push"])
    st["pending_push"] = []
    save_state(st)
    return pushed


def create_config_repo(data: dict[str, Any]) -> dict[str, Any]:
    """Create the private config repo from the template, push user/, set the secrets and the crons, enable the
    workflow. Safe to run again (an existing repo is reused)."""
    from jobradar.local.sync import ensure_picks_branch

    name = (data.get("name") or "job-radar-config").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError("repo name: letters, digits, '.', '_' and '-' only")
    status = github_status()
    if not status.get("ok"):
        raise ValueError(f"GitHub CLI not ready: {status.get('fix') or 'install gh'}")
    repo = f"{status['login']}/{name}" if status.get("login") else name
    log: list[str] = []
    exists = subprocess.run([_gh_exe(), "repo", "view", repo], capture_output=True).returncode == 0
    if not exists:
        gh(["repo", "create", repo, "--template", data.get("template") or TEMPLATE_REPO, "--private"])
        log.append(f"created {repo} (private) from {data.get('template') or TEMPLATE_REPO}")
        for _ in range(10):                         # the template copy takes a moment
            if subprocess.run([_gh_exe(), "api", f"repos/{repo}/contents/{WORKFLOW}"], capture_output=True).returncode == 0:
                break
            import time

            time.sleep(2)
    st = load_state()
    st["repo"] = repo
    save_state(st)
    write_user_yaml("settings.yaml", {"config_repo": repo})
    pushed = push_user_files(repo)
    log.append(f"pushed {len(pushed)} file(s)")
    us = config.user_settings()
    wf = gh(["api", "-H", "Accept: application/vnd.github.raw", f"repos/{repo}/contents/{WORKFLOW}"])
    new_wf = workflow_with_package(workflow_with_crons(wf, us.get("run_at") or "02:00", us.get("timezone") or "UTC"))
    if new_wf != wf:
        _put_file(repo, WORKFLOW, new_wf.encode(), "Set the overnight run time and the Job Radar version")
        log.append("set the overnight run time")
    secrets = secrets_to_set()
    for k, v in secrets.items():
        gh(["secret", "set", k, "-R", repo, "--body", v])
    log.append(f"set {len(secrets)} secret(s)")
    gh(["workflow", "enable", "jobradar.yml", "-R", repo], check=False)
    ensure_picks_branch(repo)
    st = load_state()
    st["pending_push"] = []
    save_state(st)
    return {"repo": repo, "pushed": pushed, "secrets": sorted(secrets), "kept_local": list(LOCAL_ONLY), "log": log,
            "url": f"https://github.com/{repo}"}


def _gh_exe() -> str:
    from jobradar.local.sync import gh_path

    return gh_path() or "gh"


# ------------------------------------------------------------------ first run
FAST_SOURCES = ("arbeitnow", "remotive", "remoteok", "jobicy", "himalayas", "weworkremotely", "workingnomads")


def dry_run_preview(limit_sources: int = 4) -> dict[str, Any]:
    """Fetch a few fast sources on this computer into a scratch database and show what tonight's shortlist would
    look like (nothing is saved in state.db)."""
    import tempfile

    from jobradar.pipeline.run import run_source
    from jobradar.pipeline.shortlist import build_shortlist
    from jobradar.sources.registry import load_specs
    from jobradar.store.sqlite import SQLiteStore

    specs = load_specs()
    ids = [s for s in FAST_SOURCES if s in specs][:limit_sources]
    with tempfile.TemporaryDirectory() as tmp:
        store = SQLiteStore(Path(tmp) / "preview.db")
        runs = [run_source(sid, store, trigger="preview").to_dict() for sid in ids]
        res = build_shortlist(store, dry_run=True)
        store.close()
    return {"sources": [{"id": r["source_id"], "status": r["status"], "fetched": r["fetched"], "matched": r["matched"]}
                        for r in runs], "preview": res.get("preview") or [], "would_shortlist": res.get("would_shortlist")}


def start_first_run() -> dict[str, Any]:
    st = load_state()
    repo = st.get("repo") or config.user_settings().get("config_repo")
    if not repo:
        raise ValueError("create the config repo first")
    gh(["workflow", "run", "jobradar.yml", "-R", repo, "-f", "job=daily"])
    return {"repo": repo, "actions": f"https://github.com/{repo}/actions"}


def first_run_status() -> dict[str, Any]:
    from jobradar.local.sync import workflow_runs

    runs = workflow_runs(limit=1)
    return runs[0] if runs else {}


def install_extension() -> Path:
    """Copy the Chrome extension shipped in the package to ~/.job-radar/extension (load it unpacked from there)."""
    import shutil

    src = Path(__file__).resolve().parent / "extension"
    dst = local_dir() / "extension"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return dst


def upgrade_to_full() -> dict[str, Any]:
    """Lite → Full in the same config repo: modules on, Full-only steps reopened. Jobs and picks are kept."""
    write_user_yaml("settings.yaml", {"mode": "full", "modules": {"ats": True, "screen": True, "bank": True}})
    st = load_state()
    st["mode"] = "full"
    for sid in (*FULL_ONLY, "llm", "experience", "summary"):
        st["done"].pop(sid, None)
        st["skipped"] = [s for s in st["skipped"] if s != sid]
    save_state(st)
    return {"mode": "full", "next": next_step(st)}
