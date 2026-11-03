"""Prep one job for applying: route → read the form → match questions to the answer bank → company notes →
per-job drafts of "why us" / cover-letter answers → effort + CV. The result is kept in local.db and drives the app.

A form the app can't read (LinkedIn, unknown careers pages: most jobs) gets no answers and no LLM call: prep only
records that, and the app asks for the form's URL or its pasted questions, which are then prepped like a read form."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any, Callable

from jobradar.apply.bank import LINKS, SENSITIVE_IDS, Answer, Bank, MatchResult, match_form, norm, render, slug
from jobradar.apply.forms import FormSpec, Question, fetch_form
from jobradar.apply.profile import avoid_phrases, background_text, cv_choice, detail_phrases, draft_style
from jobradar.apply.route import Route, resolve, route_for_url
from jobradar.http.client import BudgetExceeded, PoliteClient
from jobradar.llm.client import BudgetExhausted, LLMClient, LLMError
from jobradar.normalize.text import clip
from jobradar.settings import get_settings

log = logging.getLogger(__name__)

NOTES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"facts": {"type": "array", "items": {"type": "string"}},
                   "instructions": {"type": "array", "items": {"type": "string"}}},
    "required": ["facts", "instructions"], "additionalProperties": False,
}
NOTES_SYSTEM = """From a job posting, list at most 3 short, concrete facts about the company or role that a candidate could
refer to in "why us" answers (product, customers, mission, stack, team). Facts only, no opinions, no answer text.
instructions: ONLY explicit directions to applicants written in the posting, close to verbatim, e.g. "Start your
cover letter with the word BANANA" or "Include a link to your GitHub". Requirements, skills, duties or what to
emphasise are NOT instructions. Most postings have none: then return []. Plain text, no markdown."""

# Per-job drafts: "company" rows ("why us", "why this role"), the cover letter and free-text questions the bank has no
# Approved answer for are written fresh for each posting. They land as "adjust" (Worth adjusting) for the candidate to
# edit; nothing is saved to the bank.
DRAFT_IDS = frozenset({"cover_letter"})
DRAFT_TYPES = ("text", "textarea")
PLACEHOLDER = re.compile(r"^[ \t]*\[(?:your )?(?:full )?name\][ \t]*$", re.I | re.M)   # models still sign "[Name]"
# A question asking for one story may go into detail; every other drafted question gets a high-level overview
EXAMPLE_ASK = re.compile(r"\b(?:a time|(?:describe|tell us about) an?|an? (?:recent |specific |concrete )?"
                         r"(?:example|situation|case)|examples|"
                         r"walk (?:us|me) through|how did you|you(?:'ve| have)? (?:built|owned|defined|designed|led|"
                         r"fixed|debugged)|the (?:largest|biggest|hardest)|proud of)\b", re.I)
DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answers": {"type": "array", "items": {
        "type": "object", "properties": {"key": {"type": "string"}, "answer": {"type": "string"}},
        "required": ["key", "answer"], "additionalProperties": False}}},
    "required": ["answers"], "additionalProperties": False,
}
DRAFT_SYSTEM = """You draft job-application answers in the candidate's own voice; the candidate edits them before sending.
- Use ONLY facts from "Candidate background" (with its "Candidate's projects"), "Candidate's earlier answers" and
  "Candidate's notes". Never invent employers, tools, results, schedules, users or numbers (no "~80%", "nightly",
  "month-end" unless written there). Don't stretch a fact to fit the posting (sales data is not "HR-adjacent"; a sales
  tool doesn't improve "hiring").
- Build every answer from "Candidate's projects": the projects that match what the posting stresses most, main
  projects first. A "small fix" is only for a question that asks for exactly that kind of story.
- "Candidate's earlier answers" are a fallback. Reuse one when a question asks the same thing. Otherwise use it only
  when "Candidate's projects" has nothing on the topic, and never carry its details (matching rules, time windows,
  edge cases) into another answer.
- Every question has an "Answer type". "overview" (cover letters, why us, motivation and other general questions):
  a high-level summary for a reader with no context. Say what you built, for whom and what they used it for, in words
  a recruiter understands. No implementation details: no join keys, matching rules, time windows, edge cases, tool
  internals. Describe each project as a whole in one or two sentences; don't list the separate automations or steps
  inside it or the data problem it fixed. "specific example" (the question asks for one time, project or piece of work): tell that one story with
  the details that make it credible, still in plain words.
- If a question asks for experience the background doesn't show (e.g. a domain like HR data), say so in one short
  clause and answer with the closest real project, described accurately.
- Tie two or three specific things from the posting (product, customers, problems, stack) to real projects.
- First person, plain and direct, in the voice described in "Writing style" below. No flattery.
- Follow "Candidate's rules" strictly, and the candidate's notes for that question. Write in the posting's language
  if it is English or Polish, else English.
- Length: stay under the character limit when one is given. Otherwise about 80-150 words for short questions and
  150-220 words for a cover letter (no address block; open with a one-line greeting to the company's team).
- No sign-off, signature or placeholders such as [Name]: the candidate adds those.
- Plain text only: form fields don't render markdown, so no **bold**, headings or bullet symbols.
- If the answer is a personal detail, date or number the background doesn't give (address, birth date, start date,
  an exact count), return an empty answer for that key; the candidate fills it in.
Return one entry per question key."""
PASTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {
        "type": "object", "properties": {"label": {"type": "string"},
                                         "type": {"type": "string", "enum": ["text", "textarea", "select", "boolean"]},
                                         "options": {"type": "array", "items": {"type": "string"}}},
        "required": ["label", "type", "options"], "additionalProperties": False}}},
    "required": ["questions"], "additionalProperties": False,
}
PASTE_SYSTEM = """The user copied the questions of a job-application form (LinkedIn Easy Apply, a company careers page)
as plain text. Return each question the applicant must answer, in order, with the label copied verbatim (drop
"Required", "*", character counters and "Select an option"). type: textarea for open questions that need sentences
(why, describe, tell us, cover letter), select when choices are listed (copy them into options), boolean for yes/no,
text for short values (name, years, numbers, URLs). Leave out headings, help text, buttons and the resume upload."""
PASTE_NOISE = re.compile(r"^(required|\*|select an option|next|review|submit( application)?|back|\d+\s*/\s*\d+)$", re.I)
NUMBER = re.compile(r"\d[\d,.]*\s*%?")
Step = Callable[[str], None]       # progress callback: the app shows each step while a prep runs


def _quiet(_: str) -> None:
    pass


class PrepHttp:
    """Polite clients for prep: JSON APIs (Greenhouse, Ashby, Workable) and robots-respecting web pages
    (Lever apply page, Recruitee, eRecruiter, aggregator pages for unknown routes)."""

    def __init__(self, max_requests: int = 400, transport: Any = None, sleep: bool = True):
        self.api = PoliteClient("apply_prep", rps=1.0, max_requests=max_requests, transport=transport, sleep=sleep)
        self.web = PoliteClient("apply_prep_web", rps=1.0, max_requests=max_requests, respect_robots=True,
                                browser=True, transport=transport, sleep=sleep)

    def for_vendor(self, vendor: str) -> PoliteClient:
        return self.web if vendor in ("lever", "recruitee", "erecruiter", "traffit") else self.api

    def close(self) -> None:
        self.api.close()
        self.web.close()

    def __enter__(self) -> "PrepHttp":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def load_cached(job_id: str) -> dict[str, Any] | None:
    from jobradar.local.db import open_local

    return open_local().prep(job_id)


def save_cached(prep: dict[str, Any]) -> None:
    from jobradar.local.db import open_local

    open_local().save_prep(prep)


def reserve_prep() -> None:
    """Daily cap on preps (JR_PREP_DAILY_CAP, default 100), counted in ~/.job-radar/cache/prep_usage.json."""
    cap = int(os.environ.get("JR_PREP_DAILY_CAP", "100"))
    path = get_settings().cache_dir / "prep_usage.json"
    today = date.today().isoformat()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    n = int(data.get("count") or 0) if data.get("date") == today else 0
    if cap > 0 and n >= cap:
        raise BudgetExhausted(f"prep daily cap of {cap} reached")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": today, "count": n + 1}))


def job_from_row(row: dict[str, Any], local: dict[str, Any] | None = None) -> dict[str, Any]:
    """A shortlist row from state.db (or a hand-added job from local.db) → the job dict prep works on."""
    local = local or {}
    return {"job_id": row.get("job_id") or local.get("job_id") or "", "title": row.get("title") or local.get("title") or "",
            "company": row.get("company") or local.get("company") or "", "family": row.get("family") or local.get("family"),
            "route": row.get("route_vendor") or "unknown",
            "apply_url": local.get("apply_url") or row.get("route_url") or row.get("url") or local.get("url") or "",
            "posting_url": row.get("posting_url") or local.get("posting_url") or local.get("url") or "",
            "instructions": list(row.get("instructions") or []), "summary": row.get("summary") or "",
            "fit": row.get("fit"), "description": row.get("description") or local.get("description") or ""}


def route_for_job(job: dict[str, Any], http: PrepHttp | None) -> Route:
    route = route_for_url(job.get("apply_url"), "shortlist") or route_for_url(job.get("posting_url"), "shortlist")
    if route is None:
        route = Route("unknown", job.get("apply_url") or job.get("posting_url") or "", via="shortlist")
    if route.vendor == "unknown" and http is not None:
        route = resolve(route, http.web)
    return route


def company_notes(text: str, client: LLMClient) -> dict[str, list[str]]:
    if not text.strip():
        return {"facts": [], "instructions": []}
    try:
        out = client.json(f"Job posting:\n{clip(' '.join(text.split()), 6000)}", NOTES_SCHEMA, name="notes",
                          system=NOTES_SYSTEM, max_tokens=800)
    except LLMError as exc:
        log.warning("company notes failed: %s", type(exc).__name__)
        return {"facts": [], "instructions": []}
    plain = lambda s: clip(" ".join(str(s).replace("**", "").split()), 300)  # noqa: E731 - shown as plain text
    return {"facts": [plain(f) for f in out.get("facts") or []][:3],
            "instructions": [plain(i) for i in out.get("instructions") or [] if str(i).strip()][:5]}


def tailored(it: dict[str, Any], bank: Bank) -> bool:
    """A free-text question drafted per job: a "company" bank row, the cover letter, a job-specific question, or one
    the bank has no Approved answer for. Never contact details, salary or other sensitive facts. A cover-letter upload
    is drafted too: the app turns the draft into a PDF."""
    if it["type"] == "file" and it["answer_id"] == "cover_letter":
        return True
    if it["type"] not in DRAFT_TYPES or it["answer_id"] in SENSITIVE_IDS or it["answer_id"] == LINKS:
        return False
    a = bank.answers.get(it["answer_id"]) if it["answer_id"] else None
    if it.get("job_specific") or it["answer_id"] in DRAFT_IDS or (a is not None and a.kind == "company"):
        return True
    return it["status"] == "missing" and (a is None or a.kind in ("story", "fact"))


def overview(it: dict[str, Any], bank: Bank) -> bool:
    """A drafted question that gets a high-level answer: the cover letter, a "company" row ("why us") and any general
    question. Only a question that asks for one specific story (``EXAMPLE_ASK``) may go into implementation detail."""
    a = bank.answers.get(it["answer_id"]) if it["answer_id"] else None
    if it["answer_id"] in DRAFT_IDS or (a is not None and a.kind == "company"):
        return True
    return not EXAMPLE_ASK.search(it["label"])


def parse_pasted(text: str, client: LLMClient | None) -> list[Question]:
    """Questions copied by hand from a form we can't read → ``Question``s (keys stable per label). The LLM splits
    the paste into questions and options; without it (or if it fails), one question per non-noise line."""
    rows: list[dict[str, Any]] = []
    if client is not None and text.strip():
        try:
            out = client.json(f"Pasted form:\n{clip(text, 8000)}", PASTE_SCHEMA, name="pasted_form",
                              system=PASTE_SYSTEM, max_tokens=2000)
            rows = [r for r in out.get("questions") or [] if isinstance(r, dict) and str(r.get("label", "")).strip()]
        except LLMError as exc:
            log.warning("pasted form parse failed: %s", type(exc).__name__)
    if not rows:
        rows = [{"label": line, "type": "textarea" if len(line) > 60 else "text", "options": []}
                for line in (" ".join(x.split()) for x in text.splitlines()) if line and not PASTE_NOISE.match(line)]
    questions, seen = [], set()
    for r in rows:
        label = " ".join(str(r["label"]).split())
        key = "pasted_" + (slug(label)[:60] or "q")
        while key in seen:
            key += "_2"
        seen.add(key)
        qtype = r.get("type") if r.get("type") in ("text", "textarea", "select", "boolean") else "text"
        questions.append(Question(key, label, qtype, True, [str(o) for o in r.get("options") or []][:30]))
    return questions


def phrases_in(text: str, phrases: list[str]) -> list[str]:
    """The ``phrases`` that appear in ``text`` (case-insensitive, whole words)."""
    low = text.lower()
    return [p for p in phrases
            if (re.search(rf"(?<!\w){re.escape(p.lower())}(?!\w)", low) if p[0].isalnum() else p in text)]


def ai_sounding(text: str) -> list[str]:
    """Phrases from the style guide's avoid list that appear in ``text``."""
    return phrases_in(text, avoid_phrases())


def draft_answers(items: list[dict[str, Any]], bank: Bank, job: dict[str, Any], text: str, facts: list[str],
                  client: LLMClient) -> list[str]:
    """One LLM call drafting every tailored question for this job; drafted items become "adjust". Returns warnings."""
    todo = [it for it in items if tailored(it, bank)]
    if not todo:
        return []
    company, role = job.get("company", ""), job.get("title", "")
    stories = [f"- {a.question}: {clip(' '.join(a.answer.split()), 1200)}" for a in bank.answers.values()
               if a.kind == "story" and a.approved and a.answer_id not in DRAFT_IDS]
    questions, overviews = [], {it["key"] for it in todo if overview(it, bank)}
    for it in todo:
        a = bank.answers.get(it["answer_id"])
        notes = render(a.answer, company, role).strip() if a else ""
        limit = f" (max {it['max_length']} characters)" if it["max_length"] else ""
        kind = "overview" if it["key"] in overviews else "specific example"
        questions.append(f"### key: {it['key']}{limit}\nQuestion: {it['label']}\nAnswer type: {kind}"
                         + (f"\nCandidate's notes: {notes}" if notes else ""))
    # Earlier answers go first and the project list last, next to the questions: drafts should build on the projects
    prompt = (f"Role: {role}\nCompany: {company}\n\nJob posting:\n{clip(' '.join(text.split()), 6000)}\n\n"
              + ("Facts from the posting:\n" + "\n".join(f"- {f}" for f in facts) + "\n\n" if facts else "")
              + ("\n".join(["Candidate's earlier answers (fallback only, see the rules):", *stories]) + "\n\n"
                 if stories else "")
              + f"Candidate background:\n{background_text()}\n"
              + "\nQuestions:\n" + "\n\n".join(questions))
    try:
        style = draft_style()
        system = DRAFT_SYSTEM + (f"\n\nWriting style (follow it closely):\n{style}" if style else "")
        out = client.json(prompt, DRAFT_SCHEMA, name="drafts", system=system,
                          max_tokens=600 * len(todo) + 200, temperature=0.4)
    except LLMError as exc:
        log.warning("answer drafts failed: %s", type(exc).__name__)
        return ["Couldn't draft the per-job answers; write them by hand or re-prep."]
    drafts = {str(d.get("key")): PLACEHOLDER.sub("", str(d.get("answer") or "")).replace("**", "").strip()
              for d in out.get("answers") or [] if isinstance(d, dict)}
    known = {n.strip().rstrip(".,") for n in NUMBER.findall(prompt)}
    warnings = []
    for it in todo:
        draft = drafts.get(it["key"])
        if not draft:
            continue
        it["answer"], it["status"], it["drafted"] = draft, "adjust", True
        invented = sorted({n.strip().rstrip(".,") for n in NUMBER.findall(draft)} - known - {""})
        if invented:
            it.setdefault("warnings", []).append(f"numbers not in your background, check them: {', '.join(invented)}")
            warnings.append(f"Draft for “{clip(it['label'], 60)}” has numbers that aren't in your background: "
                            f"{', '.join(invented)}")
        ai_words = ai_sounding(draft)
        if ai_words:
            warnings.append(f"Draft for “{clip(it['label'], 60)}” uses words from the avoid list: {', '.join(ai_words)}")
        detail = phrases_in(draft, detail_phrases()) if it["key"] in overviews else []
        if detail:
            it.setdefault("warnings", []).append(f"too detailed for an overview answer: {', '.join(detail)}")
            warnings.append(f"Draft for “{clip(it['label'], 60)}” goes into detail an overview doesn't need: "
                            f"{', '.join(detail)}")
        if it["max_length"] and len(draft) > it["max_length"]:
            warnings.append(f"Draft for “{clip(it['label'], 60)}” is longer than {it['max_length']} characters")
    return warnings


def _counts(items: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        out[it["status"]] = out.get(it["status"], 0) + 1
    out["required_open"] = sum(1 for it in items if it["required"] and it["status"] in ("missing", "choose"))
    return out


def prep_job(job: dict[str, Any], bank: Bank, http: PrepHttp | None, client: LLMClient | None, *,
             posting_text: str | Callable[[], str] = "", form: FormSpec | None = None,
             draft_client: LLMClient | None = None, pasted: list[Question] | None = None,
             step: Step = _quiet) -> tuple[dict[str, Any], MatchResult | None]:
    """Return (prep dict, bank match result). No writes here; see ``apply_prep_writes``.
    ``draft_client`` writes the per-job drafts when set (JR_DRAFT_LLM_*), else ``client`` does.
    ``pasted``: questions copied by hand from a form we can't read; they are matched and drafted like a read form.
    ``step`` is told what prep is doing now (shown in the app)."""
    step("Finding the application form")
    route = route_for_job(job, http)
    if form is None and pasted:
        form = FormSpec("pasted", "", list(pasted))
    warnings = [f"Posting instruction: {i}" for i in job.get("instructions") or []]
    cv = cv_choice(job.get("family"), job.get("title", ""))
    base: dict[str, Any] = {"job_id": job["job_id"], "title": job.get("title", ""),
                            "company": job.get("company", ""), "route": route.to_dict(), "apply_url": route.url,
                            "cv": cv["path"], "cv_variant": cv["variant"], "cv_why": cv["why"],
                            "facts": [], "warnings": warnings}
    if not cv["path"]:
        warnings.append("No CV for this job yet: add one in the app (CV page) or in ~/.job-radar/cv.yaml")
    if form is None and route.readable and http is not None:
        step(f"Reading the {route.vendor} form")
        try:
            form = fetch_form(route.vendor, route.slug, route.job, http.for_vendor(route.vendor))
        except BudgetExceeded:
            form = None
            warnings.append("prep HTTP budget spent; try again later")
    if form is None:        # no questions, so nothing to answer yet: the app asks for the form's URL or questions
        base.update(prep="manual form", items=[], counts=_counts([]), effort_min=route.effort_min)
        return base, None
    if form.vendor == "pasted":
        base["pasted"] = [asdict(q) for q in form.questions]
    base["apply_url"] = form.apply_url or route.url
    step(f"Matching {len(form.questions)} questions to your answer bank")
    match = match_form(form, bank, company=job.get("company", ""), role=job.get("title", ""), client=client)
    items = match.items
    needs_notes = any(it["status"] == "adjust" or tailored(it, bank) for it in items)
    if needs_notes and client is not None:
        step("Reading the posting")
        text = form.content or (posting_text() if callable(posting_text) else posting_text)
        notes = company_notes(text, client)
        base["facts"] = notes["facts"]
        warnings += [f"Posting instruction: {i}" for i in notes["instructions"]
                     if f"Posting instruction: {i}" not in warnings]
        n = sum(1 for it in items if tailored(it, bank))
        if n:
            step(f"Drafting {n} answer{'s' if n > 1 else ''} for this job (about a minute)")
        warnings += draft_answers(items, bank, job, text, notes["facts"], draft_client or client)
    counts = _counts(items)
    effort = (route.effort_min + 4 * sum(1 for it in items if it["required"] and it["status"] == "missing")
              + 3 * counts.get("adjust", 0) + counts.get("choose", 0))
    base.update(items=items, counts=counts, effort_min=effort,
                prep="ready" if counts["required_open"] == 0 else "needs answers")
    return base, match


def recount(p: dict[str, Any], effort_base: int) -> None:
    items = p["items"]
    counts: dict[str, int] = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    counts["required_open"] = sum(1 for it in items if it["required"] and it["status"] in ("missing", "choose"))
    p["counts"] = counts
    if p.get("prep") != "manual form":
        p["prep"] = "ready" if counts["required_open"] == 0 else "needs answers"
        p["effort_min"] = (effort_base + 4 * sum(1 for it in items if it["required"] and it["status"] == "missing")
                           + 3 * counts.get("adjust", 0) + counts.get("choose", 0))


def seed_bank() -> Bank:
    """The starting bank rows as an in-memory bank (all Draft): for dry runs."""
    from jobradar.local.db import SEED_BANK

    return Bank([Answer(answer_id=i, kind=k, question=q, answer=a) for i, k, q, a in SEED_BANK])


def apply_prep_writes(db: Any, bank: Bank, prep: dict[str, Any], match: MatchResult | None) -> None:
    """Persist prep side effects in local.db: new aliases / Draft bank rows, the job's prep state and effort."""
    if match is not None:
        for answer_id, alias in match.alias_updates:
            db.add_alias(answer_id, alias)
        for a in match.new_rows:
            db.upsert_answer(a.answer_id, question=a.question, kind=a.kind, status="Draft", aliases=a.aliases)
    db.set_job(prep["job_id"], prep=prep["prep"], effort_min=prep["effort_min"])   # apply_url: only a URL the user gave
    db.save_prep(prep)


def pasted_from(prep: dict[str, Any] | None) -> list[Question] | None:
    """The questions pasted for this job earlier (kept in its cached prep), so a re-prep keeps them."""
    rows = (prep or {}).get("pasted")
    return [Question(**q) for q in rows] if rows else None


def approve_answer(db: Any, bank: Bank, it: dict[str, Any], text: str, *, company: str,
                   kind: str | None = None) -> Answer:
    """Save ``text`` as the Approved bank answer to the question ``it``: update its row or create one, with the
    form's wording as an alias. Points ``it`` at the row; the caller sets the item's answer and status."""
    aid = it.get("answer_id") or slug(it.get("canonical") or it["label"])
    existing = bank.answers.get(aid)
    kind = (existing.kind if existing else None) or kind or ("story" if it["type"] == "textarea" else "fact")
    question = (existing.question if existing else None) or it.get("canonical") or norm(it["label"], company)
    alias = norm(it["label"], company)
    aliases = list(existing.aliases if existing else [])
    if alias not in aliases and norm(question) != alias:
        aliases.append(alias)
    page_id = db.upsert_answer(aid, question=question, kind=kind, answer=text, status="Approved", aliases=aliases)
    a = existing or Answer(aid, kind, question)
    a.answer, a.status, a.aliases, a.page_id = text, "Approved", aliases, page_id
    bank.add(a)
    it["answer_id"] = aid
    return a
