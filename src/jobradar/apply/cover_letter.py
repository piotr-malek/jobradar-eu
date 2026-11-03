"""Cover letter → PDF, for forms that want it as an upload.

The letterhead (name, contact lines) comes from the CV source (``~/.job-radar/cv.yaml``), so it matches the CV.
The PDF lands in ``~/.job-radar/cache/cover_letters/<job_id>/<Name>_Cover_Letter.pdf``: the same neutral file name for
every job, like the CV.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader

from jobradar.apply import cv as cv_mod
from jobradar.settings import get_settings

_env = Environment(loader=PackageLoader("jobradar.apply", "templates"), autoescape=True,
                   trim_blocks=True, lstrip_blocks=True)
CLOSING = re.compile(r"^(best( regards| wishes)?|kind regards|warm regards|regards|(yours )?sincerely|cheers|thanks"
                     r"|z poważaniem|pozdrawiam)\s*[,.!]?$", re.I)     # a whole line: "Best regards,"


def letterhead() -> dict[str, Any]:
    try:
        src = cv_mod.load_source()
    except (OSError, ValueError):
        return {"name": "", "contact": []}
    return {"name": src.get("name") or "", "contact": src.get("contact") or []}


def file_name(name: str) -> str:
    """ASCII only ("Ana_Ferreira_Cover_Letter.pdf"): some upload forms mangle other characters, as the CV avoids too."""
    ascii_name = unicodedata.normalize("NFKD", name.translate(str.maketrans("łŁøØđĐ", "lLoOdD")))
    words = ascii_name.encode("ascii", "ignore").decode().split()
    return "_".join(words + ["Cover_Letter.pdf"])


def render_html(text: str, head: dict[str, Any], today: date | None = None) -> str:
    """Paragraphs split on blank lines; a closing ("Best regards," + name) is added when the text has none."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    signed = bool(paras) and any(CLOSING.match(line.strip()) for line in paras[-1].splitlines())
    return _env.get_template("cover_letter.html.j2").render(
        head=head, paras=[p.splitlines() for p in paras], closing=None if signed else "Best regards,",
        today=_long_date(today or date.today()))


def _long_date(d: date) -> str:
    return f"{d.day} {d:%B %Y}"          # "%-d" is not portable (Windows)


def to_pdf_bytes(html: str) -> bytes:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="networkidle")
            return page.pdf(format="A4", print_background=True,
                            margin={"top": "22mm", "bottom": "20mm", "left": "22mm", "right": "22mm"})
        finally:
            browser.close()


def write_pdf(job_id: str, text: str) -> Path:
    head = letterhead()
    target = get_settings().cache_dir / "cover_letters" / job_id / file_name(head["name"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(to_pdf_bytes(render_html(text, head)))
    return target
