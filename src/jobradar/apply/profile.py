"""Candidate profile (user/profile.yaml + local-only files in ~/.job-radar): prompt text for the screen/prep and the
CV choice per family."""

from __future__ import annotations

import re
from typing import Any


from jobradar.normalize.text import fold
from jobradar import config
from jobradar.settings import local_dir

SETUP_LABELS = {"remote_eu": "remote anywhere in the EU", "remote_country": "remote in their country",
                "hybrid": "hybrid near home", "onsite": "on-site near home"}


def load_profile() -> dict[str, Any]:
    p = dict(config.profile())
    local = config.read_yaml(local_dir() / "cv_variants.yaml")
    if local:
        p["cv_variants"] = {**(p.get("cv_variants") or {}), **local}
    return p


def profile_text(profile: dict[str, Any] | None = None) -> str:
    p = profile or load_profile()
    loc = p.get("location") or {}
    langs = config.language_levels(p)
    setups = [SETUP_LABELS.get(x, x) for x in loc.get("work_setups") or []]
    radius = loc.get("hybrid_radius_km")
    lines = [
        f"Summary: {' '.join(str(p.get('summary', '')).split())}",
        f"Priority roles: {', '.join(p.get('priority_roles') or [])}",
        f"Other target roles: {', '.join(p.get('target_roles') or [])}",
        f"Strong stack: {', '.join(p.get('strong_stack') or [])}",
        f"Nice to have: {', '.join(p.get('nice_stack') or [])}",
        f"Languages: {', '.join(f'{config.LANG_NAMES.get(k, k)} ({v})' for k, v in (p.get('languages') or {}).items())}"
        if langs else "Languages: English",
        f"Location: {loc.get('city', '')}, {loc.get('country', '')}; can work in: "
        f"{', '.join(loc.get('work_countries') or []) or 'the EU/EEA'}; open to: {', '.join(setups) or 'remote'}"
        + (f" (hybrid/on-site within {radius} km of home)" if radius and {'hybrid', 'onsite'} & set(loc.get('work_setups') or []) else "")
        + (f"; would relocate to: {', '.join(loc.get('relocate_to'))}" if loc.get("relocate_to") else "; not relocating"),
        f"Contracts: {', '.join(p.get('contracts') or [])}",
    ]
    if p.get("level"):                        # stated, so the screen doesn't guess a seniority (it guessed senior)
        lines.insert(1, f"Level: {' '.join(str(p['level']).split())}")
    hints = p.get("watch_out_hints") or []
    if hints:
        lines.append("Worth flagging: " + "; ".join(hints))
    return "\n".join(lines)


def background_text(profile: dict[str, Any] | None = None) -> str:
    """Summary, stack, CV highlights, the project list and the candidate's drafting rules: what per-job drafts may use."""
    p = profile or load_profile()
    lines = [f"Summary: {' '.join(str(p.get('summary', '')).split())}",
             f"Strong stack: {', '.join(p.get('strong_stack') or [])}"]
    lines += [f"- {' '.join(str(h).split())}" for h in p.get("highlights") or []]
    projects = experience().strip()
    if projects:
        lines += ["Candidate's projects:", projects]
    rules = p.get("draft_rules") or []
    if rules:
        lines += ["Candidate's rules:", *(f"- {' '.join(str(r).split())}" for r in rules)]
    return "\n".join(lines)


def experience() -> str:
    """~/.job-radar/experience.md: the candidate's projects in tiers (main, supporting, small fixes) for per-job
    drafts ("" if missing). Kept apart from `highlights` because it is long and says how to pick from it."""
    return config.local_text("experience.md")


def draft_style() -> str:
    """~/.job-radar/draft_style.md: voice rules, words to avoid and examples for per-job drafts ("" if missing)."""
    return config.local_text("draft_style.md")


def _phrase_list(heading: str, style: str | None) -> list[str]:
    """The comma-separated list under ``## <heading>`` in the style guide."""
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", draft_style() if style is None else style,
                  re.M | re.S)
    return [p.strip() for p in (m.group(1) if m else "").replace("\n", " ").split(",") if p.strip()]


def avoid_phrases(style: str | None = None) -> list[str]:
    """The comma-separated list under "## Words and phrases to avoid" in the style guide."""
    return _phrase_list("Words and phrases to avoid", style)


def detail_phrases(style: str | None = None) -> list[str]:
    """Phrases that show an overview answer (cover letter, "why us") went into implementation detail."""
    return _phrase_list("Too detailed for an overview answer", style)


def cv_options(profile: dict[str, Any] | None = None) -> dict[str, str]:
    """Named CV variants with a path set (without the `default` alias)."""
    cvs = cv_variants(profile)
    return {k: v for k, v in cvs.items() if v and k != "default"}


def cv_choice(family: str | None, title: str = "", profile: dict[str, Any] | None = None) -> dict[str, str]:
    """Pick the CV for a job: {variant, path, why}.

    A specialty family from the matcher (gtm/ai/devrel) wins. Otherwise the title's keywords
    (`cv_title_keywords`, first variant listed wins) catch mixed titles like "AI Data Engineer" that the matcher
    files under core. Then the family's own CV, then `default`.
    """
    p = profile or load_profile()
    cvs = cv_variants(p)
    keywords: dict[str, list[str]] = p.get("cv_title_keywords") or {}
    if family in keywords and family in cvs:
        return {"variant": family, "path": cvs[family], "why": f"job family {family}"}
    t = fold(title)
    for name, words in keywords.items():
        hit = re.search(rf"(?<![a-z0-9])(?:{'|'.join(words)})(?![a-z0-9])", t) if name in cvs and words else None
        if hit:
            return {"variant": name, "path": cvs[name], "why": f"title mentions “{hit.group(0)}”"}
    if family in cvs:
        return {"variant": family, "path": cvs[family], "why": f"job family {family}"}
    if cvs.get("default"):
        same = next((k for k, v in cvs.items() if k != "default" and v == cvs["default"]), "default")
        return {"variant": same, "path": cvs["default"], "why": "default"}
    return {"variant": "", "path": "", "why": "no CV configured"}


def cv_variants(profile: dict[str, Any] | None = None) -> dict[str, str]:
    """{variant: PDF path}: profile/cv_variants.yaml entries, plus PDFs `jr cv` rendered into ~/.job-radar/out/cv."""
    p = profile or load_profile()
    out = {k: v for k, v in (p.get("cv_variants") or {}).items() if v}
    root = local_dir() / "out" / "cv"
    if root.is_dir():
        for d in sorted(root.iterdir()):
            pdf = next(iter(sorted(d.glob("*.pdf"))), None) if d.is_dir() else None
            if pdf and d.name not in out:
                out[d.name] = str(pdf)
    if out and "default" not in out:
        out["default"] = out.get("core") or next(iter(out.values()))
    return out
