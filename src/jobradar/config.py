"""Config loader: package defaults < role packs < the user's profile < the user's overrides.

User files (``settings.user_dir()``, all optional):

* ``settings.yaml`` - mode (lite/full), modules, time zone, run time, run budgets
* ``profile.yaml``  - summary, level, target roles, stack, languages, location, contracts (no contact details)
* ``roles.yaml``    - role packs, seniority range, must-have / never words, extra titles
* ``sources.yaml``  - target countries, sources switched on/off, per-source overrides
* ``llm.yaml``      - provider and model per step
* ``matching.yaml`` - any matching rule, deep-merged last
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from jobradar.settings import PACKAGE_DIR, local_dir, user_dir

PACKS_DIR = PACKAGE_DIR / "packs"
SENIORITY_LEVELS = ("intern", "junior", "mid", "senior", "lead", "principal")
CEFR = {"a1": 1, "a2": 2, "b1": 3, "b2": 4, "c1": 5, "c2": 6, "native": 6, "fluent": 5, "professional": 5,
        "business": 4, "intermediate": 3, "conversational": 3, "basic": 2, "beginner": 1}
LANG_CODES = {"english": "en", "polish": "pl", "german": "de", "french": "fr", "dutch": "nl", "spanish": "es",
              "italian": "it", "portuguese": "pt", "swedish": "sv", "danish": "da", "norwegian": "no", "finnish": "fi",
              "czech": "cs", "hungarian": "hu", "romanian": "ro", "slovak": "sk", "greek": "el", "estonian": "et",
              "latvian": "lv", "lithuanian": "lt", "bulgarian": "bg", "croatian": "hr", "slovenian": "sl",
              "ukrainian": "uk", "russian": "ru", "catalan": "ca"}
LANG_NAMES = {v: k.capitalize() for k, v in LANG_CODES.items()}
# languages the detector can tell apart (normalize/lang.py); one required at working level that the user doesn't speak
# drops the job
DETECTABLE = ("en", "pl", "de", "fr", "nl", "es", "it", "pt", "sv", "da", "no", "fi", "cs", "hu", "ro")


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# ------------------------------------------------------------------ user files
@lru_cache(maxsize=1)
def user_settings() -> dict[str, Any]:
    s = read_yaml(user_dir() / "settings.yaml")
    s["mode"] = MODE_ALIASES.get(s.get("mode") or "radar", s.get("mode") or "radar")
    mods = s.setdefault("modules", {})
    apply_tier = s["mode"] == "apply"
    for name in ("ats", "screen", "notion", "weekly"):          # Radar: fetch, review, Notion, stats
        mods.setdefault(name, True)
    mods.setdefault("bank", apply_tier)                          # Radar + Apply: the app and its apply kit
    mods.setdefault("app", apply_tier)
    mods.setdefault("ats_lite_tier_a", False)
    return s


MODE_ALIASES = {"lite": "radar", "full": "apply"}               # names before the 2026-11 tier rework
MODE_LABELS = {"radar": "Radar", "apply": "Radar + Apply"}


def mode() -> str:
    """radar (fetch → AI review → Notion) or apply (adds the local app, answer bank, prep and auto-fill)."""
    return user_settings()["mode"]


def module_on(name: str) -> bool:
    return bool(user_settings()["modules"].get(name))


@lru_cache(maxsize=1)
def profile() -> dict[str, Any]:
    return read_yaml(user_dir() / "profile.yaml")


@lru_cache(maxsize=1)
def roles() -> dict[str, Any]:
    return read_yaml(user_dir() / "roles.yaml")


@lru_cache(maxsize=1)
def user_sources() -> dict[str, Any]:
    return read_yaml(user_dir() / "sources.yaml")


@lru_cache(maxsize=1)
def llm_config() -> dict[str, Any]:
    return read_yaml(user_dir() / "llm.yaml")


def local_text(name: str) -> str:
    """A local-only text file in ``~/.job-radar`` (experience.md, draft_style.md); "" when missing."""
    path = local_dir() / name
    return path.read_text(encoding="utf-8") if path.is_file() else ""


# ------------------------------------------------------------------ role packs
def available_packs() -> dict[str, dict[str, Any]]:
    return {p.stem: read_yaml(p) for p in sorted(PACKS_DIR.glob("*.yaml"))}


def active_pack_names() -> list[str]:
    names = roles().get("packs")
    return list(names) if names else sorted(available_packs())


def fold_title(title: str) -> str:
    from jobradar.normalize.text import fold

    t = fold(title or "")
    t = re.sub(r"\((?:m|w|f|d|h|x|k|all genders?|[mwfdhxk]/[mwfdhxk](?:/[mwfdhxk])?)\)", " ", t)
    return " ".join(t.split())


def title_pattern(title: str) -> str:
    """A job title the user typed or ticked → a matcher pattern: folded, gender suffix dropped, spaces flexible."""
    return r"\s+".join(re.escape(w) for w in fold_title(title).split())


def active_domains() -> set[str]:
    """The domains (it, business) of the active packs; a pack with no `domain` (a user's own) counts as both."""
    out: set[str] = set()
    for pack in load_packs(active_pack_names()):
        out |= {pack["domain"]} if pack.get("domain") else {"it", "business"}
    return out


def user_packs() -> dict[str, dict[str, Any]]:
    custom = user_dir() / "packs"
    return {p.stem: read_yaml(p) for p in sorted(custom.glob("*.yaml"))} if custom.is_dir() else {}


def load_packs(names: list[str] | None = None) -> list[dict[str, Any]]:
    """Packs in the order given (shipped packs, then the user's own in user/packs/; a user pack with a shipped
    pack's name replaces it)."""
    packs = {**available_packs(), **user_packs()}
    out = []
    for n in names or active_pack_names():
        if n not in packs:
            raise KeyError(f"unknown role pack {n!r}; available: {', '.join(sorted(packs))}")
        out.append({"name": n, **packs[n]})
    return out


def merge_packs(packs: list[dict[str, Any]]) -> dict[str, Any]:
    """Families in pack order (first match wins), stack weights (highest wins), search terms, board maps."""
    families: dict[str, Any] = {}
    weights: dict[str, int] = {}
    patterns: dict[str, str] = {}
    terms: dict[str, list[str]] = {}
    boards: dict[str, Any] = {}
    priority: dict[str, int] = {}
    labels: dict[str, str] = {}
    discipline_ok: list[str] = []
    allow: list[str] = []
    title_bonus: dict[str, int] = {}
    for pack in packs:
        title_bonus.update(pack.get("title_bonus") or {})
        fam_ex = pack.get("family_exclude") or []
        fam_ex_ok = pack.get("family_exclude_ok") or []
        for name, fam in (pack.get("families") or {}).items():
            fam = dict(fam)
            fam.setdefault("pack", pack["name"])
            if fam_ex and "exclude" not in fam:
                fam["exclude"] = fam_ex
                fam["exclude_ok"] = fam_ex_ok
            families[name] = fam
            priority[name] = int(fam.get("priority", 30))
            labels[name] = fam.get("label", "")
        st = pack.get("stack") or {}
        for k, w in (st.get("weights") or {}).items():
            weights[k] = max(weights.get(k, w), w)
        patterns.update(st.get("patterns") or {})
        for lang, ts in (pack.get("search_terms") or {}).items():
            terms.setdefault(lang, [])
            terms[lang] += [t for t in ts if t not in terms[lang]]
        for board, spec in (pack.get("boards") or {}).items():
            cur = boards.setdefault(board, {})
            for k, v in spec.items():
                if isinstance(v, list):
                    cur[k] = cur.get(k, []) + [x for x in v if x not in cur.get(k, [])]
                else:
                    cur.setdefault(k, v)
        discipline_ok += pack.get("discipline_ok_if") or []
        allow += [a for a in pack.get("allow_disciplines") or [] if a not in allow]
    return {"families": families, "weights": weights, "patterns": patterns, "search_terms": terms,
            "boards": boards, "priority": priority, "labels": labels, "discipline_ok": discipline_ok,
            "title_bonus": title_bonus, "allow_disciplines": allow}


@lru_cache(maxsize=1)
def pack_bundle() -> dict[str, Any]:
    return merge_packs(load_packs())


def search_terms(langs: list[str] | None = None, limit: int | None = None) -> list[str]:
    """Keyword queries for boards that search by text: English first, then the given languages."""
    terms = pack_bundle()["search_terms"]
    out: list[str] = []
    for lang in ["en", *(langs or [])]:
        out += [t for t in terms.get(lang, []) if t not in out]
    return out[:limit] if limit else out


def board_map(board: str) -> dict[str, Any]:
    return pack_bundle()["boards"].get(board, {})


# ------------------------------------------------------------------ profile-derived rules
def lang_code(name: str) -> str:
    n = name.strip().lower()
    return LANG_CODES.get(n, n[:2])


def language_levels(p: dict[str, Any] | None = None) -> dict[str, int]:
    """{iso code: level 1-6} from profile.languages ({en: C1} or {English: fluent})."""
    p = profile() if p is None else p
    out = {}
    for name, level in (p.get("languages") or {}).items():
        out[lang_code(str(name))] = CEFR.get(str(level).strip().lower(), 4)
    return out or {"en": 5}


def language_rules(p: dict[str, Any] | None = None) -> dict[str, Any]:
    levels = language_levels(p)
    working = sorted(c for c, lv in levels.items() if lv >= 4)
    some = sorted(c for c, lv in levels.items() if 2 <= lv < 4)
    return {
        "allowed_posting_languages": working or ["en"],
        "required_drop": [c for c in DETECTABLE if c not in working],
        "plus_tag": {c: f"{LANG_NAMES.get(c, c)} a plus" for c in some},
    }


def location_rules(p: dict[str, Any] | None = None) -> dict[str, Any]:
    """profile.location → the matcher's location settings (see normalize/location.py:LocationProfile)."""
    p = profile() if p is None else p
    loc = p.get("location") or {}
    setups = set(loc.get("work_setups") or ["remote_eu", "remote_country", "hybrid"])
    return {
        "home_city": loc.get("city") or "",
        "home_country": (loc.get("country") or "").upper()[:2] or None,
        "home_lat": loc.get("lat"),
        "home_lon": loc.get("lon"),
        "hybrid_radius_km": loc.get("hybrid_radius_km", 30),
        "home_aliases": loc.get("aliases") or [],
        "remote_eu": "remote_eu" in setups,
        "remote_country": "remote_country" in setups or "remote_eu" in setups,
        "hybrid": "hybrid" in setups,
        "onsite": "onsite" in setups,
        "relocate_to": [c.upper() for c in (loc.get("relocate_to") or [])],
        "work_countries": [c.upper() for c in (loc.get("work_countries") or [])],
    }


def seniority_rules(r: dict[str, Any] | None = None) -> dict[str, Any]:
    r = roles() if r is None else r
    s = r.get("seniority") or {}
    lo = s.get("min", "junior")
    hi = s.get("max", "senior")
    return {"min": lo if lo in SENIORITY_LEVELS else "junior", "max": hi if hi in SENIORITY_LEVELS else "senior"}


# ------------------------------------------------------------------ the matcher's config
def matching_config(*, p: dict[str, Any] | None = None, r: dict[str, Any] | None = None,
                    packs: list[str] | None = None, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    base = read_yaml(PACKAGE_DIR / "matching.yaml")
    r = roles() if r is None else r
    bundle = merge_packs(load_packs(packs or r.get("packs") or None)) if (packs or r.get("packs")) else pack_bundle()
    cfg = copy.deepcopy(base)
    cfg["title"]["families"] = bundle["families"]
    # a pack can take back roles the base config calls "never IT" (e.g. sales, customer success)
    cfg["title"]["exclude"]["discipline_hard"] = [d for d in cfg["title"]["exclude"].get("discipline_hard", [])
                                                  if d not in bundle["allow_disciplines"]]
    cfg["title"]["exclude"]["discipline_ok_if"] = list(dict.fromkeys(
        cfg["title"]["exclude"].get("discipline_ok_if", []) + bundle["discipline_ok"]))
    cfg["stack"] = {"weights": bundle["weights"], "patterns": bundle["patterns"]}
    cfg["priority"]["family"] = {**cfg["priority"].get("family", {}), **bundle["priority"]}
    cfg["priority"]["title_bonus"] = {**cfg["priority"].get("title_bonus", {}), **bundle["title_bonus"]}
    cfg["family_labels"] = bundle["labels"]
    cfg["seniority_range"] = seniority_rules(r)
    cfg["user_titles"] = {
        "never": r.get("never") or [],
        "never_unless": r.get("never_unless") or [],
        "keep": r.get("keep_titles") or [],
        "must_have": r.get("must_have") or [],
    }
    extra = list(r.get("extra_titles") or []) + [title_pattern(t) for t in r.get("position_titles") or []]
    cfg["user_titles"]["never"] = list(cfg["user_titles"]["never"]) + [
        title_pattern(t) for t in r.get("position_skip") or [] if title_pattern(t) not in extra]
    if extra:
        cfg["title"]["families"] = {"custom": {"tier": "core", "any": extra, "priority": 40, "label": "",
                                               "pack": "custom", "manager_ok": True}, **cfg["title"]["families"]}
        cfg["priority"]["family"]["custom"] = 40
        hard = cfg["title"]["exclude"].get("discipline_hard") or []     # a title the user asked for is never "not IT"
        cfg["title"]["exclude"]["discipline_hard"] = [
            d for d in hard if not any(re.search(rf"(?<![a-z0-9])(?:{d})(?![a-z0-9])", fold_title(t))
                                       for t in r.get("position_titles") or [])]
    cfg["location"] = {**cfg.get("location", {}), **location_rules(p)}
    cfg["language"] = {**cfg.get("language", {}), **language_rules(p)}
    cfg = deep_merge(cfg, read_yaml(user_dir() / "matching.yaml"))
    if overrides:
        cfg = deep_merge(cfg, overrides)
    return cfg


def rule_version(cfg: dict[str, Any]) -> str:
    raw = json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str).encode()
    return f"{cfg.get('label', 'v')}-{hashlib.sha256(raw).hexdigest()[:8]}"


def clear_caches() -> None:
    for fn in (user_settings, profile, roles, user_sources, llm_config, pack_bundle):
        fn.cache_clear()


def fold_list(words: list[str]) -> list[str]:
    from jobradar.normalize.text import fold

    return [fold(w) for w in words]


def safe_regex(pattern: str) -> bool:
    try:
        re.compile(pattern)
        return True
    except re.error:
        return False
