"""UN/INGO grade strings → ``grade:<value>`` tags (shared by the purpose-track adapters)."""

from __future__ import annotations

import re

# P-3 / P3 / IP3 / NO-B / NOA / GS-6 / G-6 / D-1 / NPSA-8 / ICS 09 / L5 / FG IV / AD6 / AST3
_GRADE = re.compile(
    r"^(?P<k>P|D|G|GS|GF|NO|NPSA|IP|ICS|FS|L|SB|SC|AD|AST|FG)[\s-]?(?P<v>\d{1,2}|[A-D]|IV|III|II|I)\b", re.I
)
_CONSULT = re.compile(r"\b(consultan\w*|roster|individual contractor|independent contractor|contractors? agreement|"
                      r"\bssa\b|special service agreement|retainer)\b", re.I)
_NON_JOB = re.compile(r"\b(intern(?:ship)?s?|unpaid|volunteers?|unv|stagiaire|pasante)\b", re.I)


def grade_tag(text: str | None) -> str | None:
    """'P-3, International Professional - …' → 'grade:P-3'; 'Consultant - Contractors Agreement' → 'grade:Consultant'."""
    if not text:
        return None
    head = text.strip().split(",")[0].strip()
    m = _GRADE.match(head)
    if m:
        k, v = m.group("k").upper(), m.group("v").upper()
        sep = {"FG": " ", "AD": "", "AST": ""}.get(k, "-")
        return f"grade:{k}{sep}{v}"
    if _CONSULT.search(text):
        return "grade:Consultant"
    return None


def is_non_job(text: str | None) -> bool:
    """Internships / volunteer / unpaid placements (not real jobs)."""
    return bool(text and _NON_JOB.search(text))
