"""Answer bank matching: form question → bank entry → verbatim answer.

Order per question: (1) deterministic rules for contact facts and files, (2) exact match of the normalised label
against bank questions and aliases, (3) ONE LLM call per form mapping the rest to bank ids (it never writes answers),
(4) select/boolean option picking — deterministic first, else one LLM call per form. Contact facts (name, e-mail,
phone, links) never reach an LLM. Only Approved answers are used; ``{company}`` / ``{role}`` are filled in.

``match_form`` has no side effects on storage: it returns the aliases to append and the Draft rows to create
(``MatchResult.alias_updates`` / ``.new_rows``) and updates the in-memory bank so the next form reuses them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from jobradar.apply.forms import FormSpec, Question
from jobradar.llm.client import LLMClient, LLMError

log = logging.getLogger(__name__)

KINDS = ("fact", "story", "company", "eeo", "consent")
PII_IDS = frozenset({"first_name", "last_name", "full_name", "email", "phone", "linkedin", "github", "website",
                     "__links__"})
# never sent to the option-picking LLM either (it may be the Mistral API): personal / negotiating facts
SENSITIVE_IDS = PII_IDS | {"current_company", "current_title", "salary_expectation", "notice_period", "city"}
SENSITIVE_KINDS = frozenset({"eeo"})
CV = "__cv__"
LINKS = "__links__"         # one field asking for several profile links: the approved ones, joined
LINK_IDS = ("linkedin", "github", "website")
SKIP = "__skip__"
OPTION_TYPES = ("select", "multiselect", "boolean")

# (answer_id, label regex, key regex, max label length for the label rule)
RULES: list[tuple[str, str, str, int]] = [
    (CV, r"\b(resume|cv|curriculum vitae)\b", r"^(resume|cv|_systemfield_resume)$", 40),
    ("first_name", r"^(first|given) name$", r"^(first_?name)$", 30),
    ("last_name", r"^(last|family) name$|^surname$", r"^(last_?name)$", 30),
    ("full_name", r"^(full )?(legal )?name$", r"^(name|_systemfield_name)$", 30),
    ("email", r"^e ?mail( address)?$", r"^(email|_systemfield_email)$", 30),
    ("phone", r"^(phone|mobile|telephone)( number)?$", r"^phone$", 30),
    (LINKS, r"linkedin.*\b(github|website|portfolio)\b|\b(github|website|portfolio)\b.*linkedin", r"$^", 120),
    ("linkedin", r"linkedin", r"urls\[linkedin\]", 40),
    ("github", r"github", r"urls\[github\]", 40),
    ("website", r"^(website|portfolio|personal website)( url| link)?$", r"urls\[portfolio\]", 40),
    ("city", r"^(current )?(location|city)$", r"^location$", 30),
    ("country", r"country (of|you) (current )?(residence|live)|country do you (currently )?live|current country of residence",
     r"$^", 80),
    ("current_company", r"^(current )?(company|employer)$", r"^org$", 30),
    ("ethnicity", r"^are you hispanic( or |/)?latino", r"^hispanic_ethnicity$", 40),
    ("education_school", r"$^", r"^school--0$", 0),
    ("education_degree", r"$^", r"^degree--0$", 0),
    ("education_discipline", r"$^", r"^discipline--0$", 0),
    ("education_start_year", r"$^", r"^start-year--0$", 0),
    ("education_end_year", r"$^", r"^end-year--0$", 0),
]


def norm(label: str, company: str | None = None) -> str:
    s = (label or "").lower().replace("✱", " ").replace("*", " ")
    if company and len(company.strip()) > 1:
        s = re.sub(re.escape(company.strip().lower()), "{company}", s)
    s = re.sub(r"[^\w\s{}]", " ", s)
    return " ".join(s.split())


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", norm(text).replace("{company}", "company")).strip("_")
    return "_".join(s.split("_")[:6]) or "question"


@dataclass
class Answer:
    answer_id: str
    kind: str
    question: str
    answer: str = ""
    short: str = ""
    aliases: list[str] = field(default_factory=list)
    status: str = "Draft"
    uses: int = 0
    page_id: str | None = None

    @property
    def approved(self) -> bool:
        return self.status == "Approved" and bool(self.answer.strip())


@dataclass
class MatchResult:
    items: list[dict[str, Any]]
    alias_updates: list[tuple[str, str]] = field(default_factory=list)   # (answer_id, alias)
    new_rows: list[Answer] = field(default_factory=list)
    llm_calls: int = 0


class Bank:
    def __init__(self, answers: list[Answer]):
        self.answers = {a.answer_id: a for a in answers}
        self._index: dict[str, str] = {}
        for a in answers:
            self._add_index(a)

    def _add_index(self, a: Answer) -> None:
        for text in [a.question, *a.aliases]:
            if text.strip():
                self._index.setdefault(norm(text), a.answer_id)

    def lookup(self, label: str, company: str | None) -> str | None:
        return self._index.get(norm(label, company))

    def add_alias(self, answer_id: str, alias: str) -> bool:
        a = self.answers.get(answer_id)
        if not a or norm(alias) in {norm(x) for x in [a.question, *a.aliases]}:
            return False
        a.aliases.append(alias)
        self._add_index(a)
        return True

    def add(self, a: Answer) -> None:
        self.answers[a.answer_id] = a
        self._add_index(a)


# ---------------------------------------------------------------- matching
def rule_for(q: Question) -> str | None:
    label = norm(q.label)
    key = (q.key or "").lower()
    for answer_id, label_rx, key_rx, max_len in RULES:
        if re.search(key_rx, key) or (len(label) <= max_len and re.search(label_rx, label)):
            if answer_id == CV and q.type != "file":
                return SKIP                       # "paste your resume" textarea next to the upload
            return answer_id
    if q.type == "file" and re.search(r"cover letter", label):
        return "cover_letter"                     # drafted per job; the app makes the PDF to upload
    return None


def _is_info(q: Question) -> bool:
    """Optional free text that is really an explanation, not a question (e.g. Recruitee '💡 Important info')."""
    return not q.required and q.type in ("text", "textarea") and "?" not in q.label and len(q.label) > 120


def render(text: str, company: str, role: str) -> str:
    return (text or "").replace("{company}", company or "").replace("{role}", role or "")


DECLINE = re.compile(r"(don'?t|do not) wish|prefer not|rather not|decline|not to (self[- ]?)?identify|not to (answer|say|disclose)",
                     re.I)


def pick_option(answer: str, options: list[str], qtype: str) -> str | None:
    a = " ".join((answer or "").lower().split())
    if not a:
        return None
    opts = options or (["Yes", "No"] if qtype == "boolean" else [])
    if DECLINE.search(a):                 # every form words "no answer" differently
        return next((o for o in opts if DECLINE.search(o)), None)
    low = [(o, " ".join(o.lower().split())) for o in opts]
    for o, lo in low:
        if lo == a:
            return o
    if qtype == "boolean" or {lo for _, lo in low} <= {"yes", "no", "true", "false"}:
        first = re.split(r"\W+", a)[0]
        want = {"yes": ("yes", "true"), "true": ("yes", "true"), "no": ("no", "false"), "false": ("no", "false")}.get(first)
        if want:
            return next((o for o, lo in low if lo in want), "Yes" if want[0] == "yes" else "No")
        return None
    word = lambda x, y: re.match(rf"{re.escape(y)}\b", x) is not None  # noqa: E731 - "no" ≠ "not applicable"
    pref = [o for o, lo in low if word(lo, a) or word(a, lo)]
    if pref:
        return max(pref, key=len)
    within = lambda x, y: re.search(rf"\b{re.escape(y)}\b", x) is not None  # noqa: E731
    cont = [o for o, lo in low if within(lo, a) or (len(lo) > 3 and within(a, lo))]
    return max(cont, key=len) if len(cont) >= 1 else None


MAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"key": {"type": "string"}, "answer_id": {"type": "string"},
                       "kind": {"type": "string",
                                "enum": ["fact", "story", "company", "eeo", "consent", "choice", "new", "job"]},
                       "canonical": {"type": "string"}},
        "required": ["key", "answer_id", "kind", "canonical"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}
MAP_SYSTEM = """You map job-application form questions to entries of a candidate's answer bank. You never write answers.
For each form question return: key (as given), answer_id (an id from the bank, or "" when none fits),
kind, and canonical (a short, company-neutral wording of the question; use {company} for the company name).
Rules:
- Map to a bank id only when the MEANING is the same, not just the topic. "Why do you want to work at X?" = why_company;
  "Describe your experience with dbt" ≠ "Do you have 3+ years of dbt?".
- Situational multiple choice ("what would you do if…") and self-assessment scales → kind "choice", answer_id "".
- A free-text question tied to THIS job that a stored answer can't serve as-is: it names the job's domain, product or
  stack ("What HR data have you modelled?"), or gives multi-part instructions ("Describe a project in three parts:
  (1)… (2)…") → kind "job", answer_id "". It is answered fresh for this application.
- A question with no bank entry that the candidate would reuse across applications → kind "new" (fact, story-like or
  company-specific questions all count), answer_id "", with a good canonical wording.
- Otherwise use the bank entry's kind."""

OPTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": {"key": {"type": "string"}, "option": {"type": "string"}},
        "required": ["key", "option"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}
OPTION_SYSTEM = """For each form field, choose the option that states the same thing as the candidate's stored answer.
Return the option text exactly as listed, or "" when no option matches the stored answer. Never guess beyond the answer."""


def _status(a: Answer | None, q: Question) -> str:
    if a is None or not a.approved:
        return "missing"
    return {"story": "bank", "company": "adjust"}.get(a.kind, "auto")


def match_form(form: FormSpec, bank: Bank, *, company: str, role: str,
               client: LLMClient | None = None) -> MatchResult:
    res = MatchResult(items=[])
    items: dict[str, dict[str, Any]] = {}
    unmatched: list[Question] = []
    for q in form.questions:
        it = {"key": q.key, "label": q.label, "type": q.type, "required": q.required, "options": q.options,
              "max_length": q.max_length, "status": "", "answer_id": "", "answer": "", "option": "", "canonical": ""}
        items[q.key] = it
        rid = rule_for(q)
        if rid == CV:
            it["status"] = "file"
            continue
        if rid == SKIP:
            it["status"] = "skip"
            continue
        if rid == LINKS:
            links = [bank.answers[i].answer for i in LINK_IDS if i in bank.answers and bank.answers[i].approved]
            it["answer_id"], it["answer"] = LINKS, ", ".join(links)
            it["status"] = "auto" if links else ("missing" if q.required else "skip")
            continue
        aid = rid or bank.lookup(q.label, company)
        if aid:
            it["answer_id"] = aid
        elif _is_info(q):
            it["status"] = "skip"
        else:
            unmatched.append(q)

    if unmatched and client is not None:
        _llm_map(unmatched, bank, items, res, company, client)

    # resolve answers + statuses
    needs_option: list[dict[str, Any]] = []
    for q in form.questions:
        it = items[q.key]
        if it["status"]:
            continue
        a = bank.answers.get(it["answer_id"]) if it["answer_id"] else None
        if it["status"] == "" and it.get("_choice"):
            it["status"] = "choose"
        else:
            it["status"] = _status(a, q)
        if a and a.approved:
            text = a.answer
            if q.max_length and len(render(text, company, role)) > q.max_length and a.short:
                text = a.short
            it["answer"] = render(text, company, role)
            if q.max_length and len(it["answer"]) > q.max_length:
                it.setdefault("warnings", []).append(f"answer longer than {q.max_length} characters")
            if q.type in OPTION_TYPES:
                opt = pick_option(it["answer"], q.options, q.type)
                if opt:
                    it["option"] = opt
                else:
                    needs_option.append(it)
        if it["status"] in ("missing", "choose") and not q.required:
            it["status"] = "skip"
        it.pop("_choice", None)

    if any(it["type"] == "file" and it["answer_id"] == "cover_letter" for it in items.values()):
        for it in items.values():       # Greenhouse's "or enter manually" twin: the upload (a PDF) carries the letter
            if it["type"] != "file" and it["answer_id"] == "cover_letter":
                it.update(answer_id="", answer="", status="skip")
    if needs_option and client is not None:
        _llm_options(needs_option, res, client, bank)
    for it in needs_option:
        if not it["option"]:
            it["status"] = "choose"
    res.items = [items[q.key] for q in form.questions]
    return res


def _llm_map(unmatched: list[Question], bank: Bank, items: dict[str, dict[str, Any]], res: MatchResult,
             company: str, client: LLMClient) -> None:
    bank_lines = "\n".join(f"{a.answer_id} | {a.kind} | {a.question}" for a in bank.answers.values())
    q_lines = "\n".join(f"{q.key} | {q.type} | {norm(q.label, company) if company else q.label}"
                        + (f" | options: {'; '.join(q.options[:8])}" if q.options else "") for q in unmatched)
    prompt = f"Answer bank (answer_id | kind | question):\n{bank_lines}\n\nForm questions (key | type | label):\n{q_lines}"
    try:
        out = client.json(prompt, MAP_SCHEMA, name="bank_map", system=MAP_SYSTEM, max_tokens=150 * len(unmatched) + 200)
        res.llm_calls += 1
    except LLMError as exc:
        log.warning("bank mapping call failed: %s", type(exc).__name__)
        return
    by_key = {q.key: q for q in unmatched}
    for r in out.get("items") or []:
        q = by_key.get(r.get("key"))
        if not q:
            continue
        it = items[q.key]
        alias = norm(q.label, company)
        aid, kind = r.get("answer_id") or "", r.get("kind") or ""
        it["canonical"] = r.get("canonical") or ""
        if kind == "job" and q.type in ("text", "textarea"):
            it["job_specific"] = True      # drafted per job in prep; no alias, no bank row
        elif aid in bank.answers and kind != "choice":
            it["answer_id"] = aid
            it["mapped_by_ai"] = True
            if bank.add_alias(aid, alias):
                res.alias_updates.append((aid, alias))
        elif kind == "choice":
            it["_choice"] = True
        elif kind == "new" or aid:
            canonical = it["canonical"] or q.label
            new_id = slug(canonical)
            while new_id in bank.answers:
                new_id += "_2"
            new_kind = "story" if q.type == "textarea" else "fact"
            if "{company}" in canonical:
                new_kind = "company"
            a = Answer(answer_id=new_id, kind=new_kind, question=canonical, aliases=[alias])
            bank.add(a)
            res.new_rows.append(a)
            it["answer_id"] = new_id


def _llm_options(pending: list[dict[str, Any]], res: MatchResult, client: LLMClient, bank: Bank) -> None:
    safe = [it for it in pending if it["answer_id"] not in SENSITIVE_IDS
            and (bank.answers.get(it["answer_id"]) or Answer("", "fact", "")).kind not in SENSITIVE_KINDS]
    if not safe:
        return
    lines = "\n".join(f"{it['key']} | {it['label']} | stored answer: {it['answer']} | options: {'; '.join(it['options'] or ['Yes', 'No'])}"
                      for it in safe)
    try:
        out = client.json(f"Fields (key | label | stored answer | options):\n{lines}", OPTION_SCHEMA, name="options",
                          system=OPTION_SYSTEM, max_tokens=60 * len(safe) + 100)
        res.llm_calls += 1
    except LLMError as exc:
        log.warning("option picking call failed: %s", type(exc).__name__)
        return
    by_key = {it["key"]: it for it in safe}
    for r in out.get("items") or []:
        it = by_key.get(r.get("key"))
        if it and r.get("option") in (it["options"] or ["Yes", "No"]):
            it["option"] = r["option"]
