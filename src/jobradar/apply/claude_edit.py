"""Hand chosen answers of one job to an AI editing session (Claude Code or any chat) for rework.

The app writes a brief to ``~/.job-radar/cache/claude/<job_id>.md`` (posting, the chosen questions with their current
answers, approved stories, where the style rules live, how to save). Then, depending on ``$JR_EDITOR``:

* ``vscode`` / ``cursor`` (or another editor scheme): opens a new Claude Code conversation in that editor through the
  extension's URI handler (``<scheme>://anthropic.claude-code/open?prompt=…``), via the system's URL opener;
* unset or ``copy``: the page shows the brief's path and a prompt to copy into any assistant.

The assistant saves approved text with ``jr answer``; the app page picks it up on reload.
"""

from __future__ import annotations

import os
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import quote

from jobradar.apply.bank import Bank
from jobradar.normalize.text import clip
from jobradar.settings import get_settings, local_dir, user_dir


def brief_path(job_id: str) -> Path:
    d = get_settings().cache_dir / "claude"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{job_id}.md"


def write_brief(p: dict[str, Any], keys: list[str], bank: Bank, posting_text: str = "") -> Path:
    """The context file for one editing session. Holds answers (personal data): it stays in the gitignored cache."""
    chosen = [it for it in p["items"] if it["key"] in keys]
    company, title, job_id = p.get("company", ""), p.get("title", ""), p["job_id"]
    stories = [f"- {a.question}: {' '.join(a.answer.split())}" for a in bank.answers.values()
               if a.kind == "story" and a.approved and a.answer.strip()]
    out = [f"# Edit application answers: {title} at {company} ({job_id})", "",
           "The user is applying for this job and wants to rework the answers below with you. They will tell you",
           "what to change; their comments are not in this file.", "",
           "## How to work",
           f"1. First read `{local_dir() / 'draft_style.md'}` (their writing rules), the `summary`, `highlights`",
           f"   and `draft_rules` in `{user_dir() / 'profile.yaml'}`, and `{local_dir() / 'experience.md'}` (their",
           "   projects and how to pick from them). These are the only facts about them you may use. Build answers",
           "   from experience.md; the approved answers below are a fallback for topics it doesn't cover.",
           "2. Ask what they want changed if they haven't said. Show the new text in the chat and wait for their OK.",
           "3. Use only facts from those files, the stories below and the posting. Never invent employers, tools,",
           "   results or numbers. Plain text: no markdown, no sign-off, no [Name]. Respect any character limit.",
           "   Cover letters, motivation and other general questions are high-level overviews a recruiter with no",
           "   context understands: what was built, for whom, what it was used for. No joins, matching rules or",
           "   edge cases unless the question asks how something was done. In a cover letter, one or two sentences",
           "   per project as a whole: never the separate automations or steps inside it, or the data problem it fixed.",
           "4. When they approve an answer, save it for this application (exact text, nothing else in the heredoc):",
           "   ```", f"   jr answer {job_id} --key <key> <<'EOF'", "   <the approved text>", "   EOF",
           "   ```",
           "   Add `--bank` only when they ask to also save it to the answer bank for future applications",
           "   (it becomes Approved there and is reused verbatim, so it must not be specific to this company).",
           "5. Tell them to reload the job page in the app (it also shows a reload notice by itself).", "",
           "## Questions to work on"]
    for it in chosen:
        limit = f" (max {it['max_length']} characters)" if it.get("max_length") else ""
        out += ["", f"### key: `{it['key']}`{limit}", f"Question: {it['label']}",
                f"Current answer ({it['status']}):", "", (it.get("option") or it.get("answer") or "(empty)").strip()]
    if p.get("facts"):
        out += ["", "## Facts about the company (from the posting)", *[f"- {f}" for f in p["facts"]]]
    instructions = [w[21:] for w in p.get("warnings") or [] if w.startswith("Posting instruction: ")]
    if instructions:
        out += ["", "## Instructions in the posting (follow them)", *[f"- {i}" for i in instructions]]
    if stories:
        out += ["", "## Their approved answers to common questions (fallback: only when experience.md has nothing)",
                *stories]
    if posting_text.strip():
        out += ["", "## Posting", "", clip(posting_text.strip(), 12000)]
    path = brief_path(job_id)
    path.write_text("\n".join(out) + "\n")
    return path


def session_prompt(brief: Path) -> str:
    return (f"Help me edit some job-application answers. Read {brief} first and follow its instructions, "
            "then ask me what to change.")


def session_url(brief: Path, scheme: str | None = None) -> str | None:
    """The editor link for a Claude Code conversation, or None when no editor is set ($JR_EDITOR unset or "copy")."""
    scheme = scheme or os.environ.get("JR_EDITOR", "")
    if not scheme or scheme == "copy":
        return None
    return f"{scheme}://anthropic.claude-code/open?prompt={quote(session_prompt(brief))}"


def open_session(url: str | None) -> bool:
    """Open the editor link with the system's URL handler; False when that isn't possible (the page shows the
    prompt to copy instead)."""
    if not url:
        return False
    try:
        return bool(webbrowser.open(url))
    except webbrowser.Error:
        return False
