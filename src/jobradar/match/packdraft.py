"""Custom role pack from a description (`jr pack draft "…"`, the wizard's roles step).

An LLM proposes families (title regexes), search terms and golden titles; the regexes are compiled and run on sample
titles (every pack's golden titles), so the user sees what the draft keeps and drops before saving it to
user/packs/<name>.yaml."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from jobradar import config
from jobradar.settings import user_dir

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "patterns": {"type": "array", "items": {"type": "string"}},
        "search_terms": {"type": "array", "items": {"type": "string"}},
        "keep": {"type": "array", "items": {"type": "string"}},
        "drop": {"type": "array", "items": {"type": "string"}},
        "allow_disciplines": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["label", "patterns", "search_terms", "keep", "drop", "allow_disciplines"],
    "additionalProperties": False,
}
SYSTEM = """You write a role pack for a job-title matcher. The matcher lowercases titles and strips accents
(ż→z, é→e), then tests each regex pattern with word boundaries added around it. Return:
- label: a short name for the roles
- patterns: 3-12 Python regexes matching the role's titles in English and the main European languages (no seniority
  words, no ^ or $; use (?:...) groups, not capturing groups)
- search_terms: 4-8 short job-board search phrases in English
- keep: 15 realistic job titles the patterns must match (some in Polish, German, Spanish, Portuguese or French)
- drop: 8 realistic titles of nearby roles they must NOT match.
- allow_disciplines: the matcher drops every title containing one of these patterns as "not an office/IT role":
  {blocked}. List, copied exactly, only the ones these roles need (e.g. "recruiter" for recruiting roles); usually
  none."""


def _alt(p: str) -> re.Pattern[str] | None:
    try:
        return re.compile(rf"(?<![a-z0-9])(?:{p})(?![a-z0-9])")
    except re.error:
        return None


def blocked_disciplines() -> list[str]:
    from jobradar.settings import PACKAGE_DIR

    base = config.read_yaml(PACKAGE_DIR / "matching.yaml")
    return list(((base.get("title") or {}).get("exclude") or {}).get("discipline_hard") or [])


def build_pack(name: str, d: dict[str, Any]) -> dict[str, Any]:
    patterns = [p for p in d.get("patterns") or [] if _alt(p)]
    allow = [a for a in d.get("allow_disciplines") or [] if a in blocked_disciplines()]
    return {"label": d.get("label") or name, "description": d.get("label") or name,
            **({"allow_disciplines": allow} if allow else {}),
            "families": {name.replace("-", "_"): {"tier": "core", "priority": 35, "label": d.get("label") or "",
                                                  "any": patterns}},
            "search_terms": {"en": list(d.get("search_terms") or [])[:8]},
            "golden": {"keep": list(d.get("keep") or []), "drop": list(d.get("drop") or [])}}


def preview(pack: dict[str, Any], titles: list[str] | None = None) -> dict[str, Any]:
    """Run the pack alone (default seniority range) on its own golden titles plus every shipped pack's titles."""
    from jobradar.match.engine import MatchEngine

    name = next(iter(pack["families"]))
    cfg = config.matching_config(r={"packs": []}, packs=[next(iter(config.available_packs()))])
    cfg["title"]["families"] = pack["families"]
    cfg["title"]["exclude"]["discipline_hard"] = [x for x in cfg["title"]["exclude"].get("discipline_hard", [])
                                                  if x not in (pack.get("allow_disciplines") or [])]
    cfg["family_labels"] = {name: pack["families"][name].get("label", "")}
    cfg["user_titles"] = {"never": [], "never_unless": [], "keep": [], "must_have": []}
    cfg["seniority_range"] = {"min": "junior", "max": "senior"}
    engine = MatchEngine.from_config(cfg)
    golden = pack.get("golden") or {}
    sample = titles if titles is not None else sorted({t for p in config.available_packs().values()
                                                       for t in (p.get("golden") or {}).get("keep", [])})
    kept = lambda t: engine.title_verdict(t).tier != "excluded"  # noqa: E731
    return {"keep_ok": [t for t in golden.get("keep", []) if kept(t)],
            "keep_missed": [t for t in golden.get("keep", []) if not kept(t)],
            "drop_ok": [t for t in golden.get("drop", []) if not kept(t)],
            "drop_leaked": [t for t in golden.get("drop", []) if kept(t)],
            "other_packs_caught": [t for t in sample if kept(t)][:40]}


def draft_pack(description: str, *, name: str = "custom", client: Any = None) -> dict[str, Any]:
    from jobradar.llm.client import LLMClient, LLMError

    own = client is None
    try:
        client = client or LLMClient("onboarding")
        system = SYSTEM.replace("{blocked}", ", ".join(f'"{x}"' for x in blocked_disciplines()))
        d = client.json(f"Roles wanted: {description}", SCHEMA, name="role_pack", system=system, max_tokens=1500)
    except LLMError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        if own and client is not None:
            client.close()
    pack = build_pack(name, d)
    if not pack["families"][name.replace("-", "_")]["any"]:
        return {"ok": False, "error": "the model returned no valid patterns", "pack": pack}
    pv = preview(pack)
    ok = len(pv["keep_ok"]) >= max(1, len(pv["keep_ok"]) + len(pv["keep_missed"])) * 0.7 and len(pv["drop_leaked"]) <= 2
    return {"ok": ok, "pack": pack, **pv}


def save_pack(pack: dict[str, Any], name: str = "custom") -> Path:
    d = user_dir() / "packs"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.yaml"
    path.write_text(yaml.safe_dump(pack, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path
