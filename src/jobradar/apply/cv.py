"""CV variants: one YAML source of facts (~/.job-radar/cv.yaml) → one PDF per job family.

The source stays on the user's computer (it holds contact details); see `jr cv --help`. A variant only picks and orders
what the source already says (headline, summary, skill groups, bullets, projects), so facts can't drift between CVs.
A bullet's `text` is a string or {default: …, <variant>: …} when a variant needs different wording of the same fact.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, PackageLoader



def default_source() -> Path:
    from jobradar.settings import local_dir

    return Path(os.environ.get("JR_CV_SOURCE") or local_dir() / "cv.yaml").expanduser()


def default_out() -> Path:
    from jobradar.settings import local_dir

    return local_dir() / "out" / "cv"

_env = Environment(loader=PackageLoader("jobradar.apply", "templates"), autoescape=True,
                   trim_blocks=True, lstrip_blocks=True)


class CVError(ValueError):
    pass


def load_source(path: Path | None = None) -> dict[str, Any]:
    return yaml.safe_load(Path(path or default_source()).read_text(encoding="utf-8"))


def _text(value: Any, variant: str) -> str:
    if isinstance(value, dict):
        return value.get(variant) or value.get("default") or ""
    return value or ""


def _pick(pool: dict[str, Any], ids: list[str], where: str) -> list[Any]:
    missing = [i for i in ids if i not in pool]
    if missing:
        raise CVError(f"{where}: unknown id(s) {missing}")
    return [pool[i] for i in ids]


def resolve(src: dict[str, Any], variant: str) -> dict[str, Any]:
    """The variant's CV as plain data (what the template renders and what tests check)."""
    v = (src.get("variants") or {}).get(variant)
    if not v:
        raise CVError(f"no variant {variant!r} in the CV source")
    skills = [{**g, "items": _text(g["items"], variant)} for g in _pick(src["skills"], v["skills"], f"{variant}.skills")]
    jobs = []
    for job in src["experience"]:
        roles = []
        for role in job["roles"]:
            ids = (v.get("bullets") or {}).get(role["id"])
            if ids is None:
                ids = role.get("default_bullets") or list(role["bullets"])
            bullets = [_text(b, variant) for b in _pick(role["bullets"], ids, f"{variant}.bullets.{role['id']}")]
            roles.append({**role, "bullets": [b for b in bullets if b]})
        jobs.append({**job, "roles": roles})
    projects = _pick(src["projects"], v["projects"], f"{variant}.projects")
    return {"name": src["name"], "contact": src["contact"], "headline": v["headline"], "summary": v["summary"],
            "skills": skills, "experience": jobs,
            "projects": [{"name": p["name"], "text": _text(p["text"], variant)} for p in projects],
            "education": src.get("education") or [], "variant": variant}


def render_html(src: dict[str, Any], variant: str) -> str:
    return _env.get_template("cv.html.j2").render(cv=resolve(src, variant))


def build(src_path: Path | None = None, variants: list[str] | None = None,
          out_dir: Path | None = None) -> dict[str, Path]:
    """Write <out_dir>/<variant>/<file_name> (PDF, plus the HTML next to it) for each variant."""
    from playwright.sync_api import sync_playwright

    src = load_source(src_path)
    out_dir = out_dir or default_out()
    names = variants or list(src["variants"])
    file_name = src.get("file_name") or "CV.pdf"  # the same neutral name for every variant: recruiters see it
    done: dict[str, Path] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        for name in names:
            html = render_html(src, name)
            target = out_dir / name / file_name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.with_suffix(".html").write_text(html, encoding="utf-8")
            page.set_content(html, wait_until="networkidle")
            page.pdf(path=str(target), format="A4", print_background=True,
                     margin={"top": "10mm", "bottom": "10mm", "left": "13mm", "right": "13mm"})
            done[name] = target
        browser.close()
    return done
