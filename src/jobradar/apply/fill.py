"""Auto-fill: what the Job Radar Chrome extension (``extension/``) gets to fill a form, and its report back.

The form is filled in the user's own Chrome, not in a browser the app drives: reCAPTCHA Enterprise (Ashby) and
hCaptcha (Lever) flag a Playwright-launched Chromium as a bot (challenge after challenge, then a rejected submit).
The job page hands ``payload`` to the extension, which opens the form in a new tab, fills it with
``extension/filler.js`` and sends the rows back for ``report``. Submit is always the user's click: the filler refuses
submit-like elements and never presses Enter.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

FILLABLE = ("auto", "bank", "adjust", "once", "file")
FIELDS = ("key", "label", "type", "status", "answer", "option", "answer_id", "required")


def _file(path: str | Path) -> dict[str, str]:
    p = Path(path)
    return {"name": p.name, "mime": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
            "b64": base64.b64encode(p.read_bytes()).decode()}


def payload(prep: dict[str, Any], letter_pdf: Callable[[str], Path]) -> dict[str, Any]:
    """The fillable questions in form order; the CV and a drafted cover letter travel as files."""
    cv = prep.get("cv") or ""
    items = []
    for it in prep.get("items") or []:
        if it.get("status") not in FILLABLE:
            continue
        row = {k: it.get(k) for k in FIELDS}
        if it["status"] == "file":
            row["file"] = _file(cv) if cv and Path(cv).is_file() else None
        elif it.get("type") == "file":             # a cover-letter upload: its draft as a PDF
            row["file"] = None
            if it.get("answer"):
                try:
                    row["file"] = _file(letter_pdf(it["answer"]))
                except Exception as exc:  # noqa: BLE001 - the report says "no file to upload"
                    log.warning("cover letter PDF failed: %s", type(exc).__name__)
        items.append(row)
    return {"job_id": prep["job_id"], "url": prep["apply_url"], "items": items}


def report(prep: dict[str, Any], rows: list[Any]) -> list[dict[str, Any]]:
    """The extension's rows in form order, plus the required questions it wasn't given (left for the user)."""
    by_key = {r.get("key"): r for r in rows if isinstance(r, dict)}
    out = []
    for it in prep.get("items") or []:
        if it.get("status") in FILLABLE:
            r = by_key.get(it["key"]) or {}
            out.append({"key": it["key"], "label": it["label"], "ok": r.get("ok") is True,
                        "how": str(r.get("how") or "not reported")[:200]})
        elif it.get("required"):
            out.append({"key": it["key"], "label": it["label"], "ok": False, "how": f"status {it['status']}"})
    return out
