"""Custom role pack drafted by an LLM: patterns compiled and previewed; blocked titles lifted only when the base
list really has them."""

from __future__ import annotations

from fakes import FakeLLM
from jobradar.llm.client import LLMClient
from jobradar.match.packdraft import blocked_disciplines, draft_pack


def test_a_recruiter_pack_lifts_the_blocked_title(tmp_path):
    reply = {"label": "Agency recruiting", "patterns": ["recruiter", "recruitment consultant", "headhunter"],
             "search_terms": ["recruiter"], "keep": ["Recruiter", "Senior Recruiter", "Recruitment Consultant",
                                                     "Headhunter"],
             "drop": ["Software Engineer", "Sales Representative"], "allow_disciplines": ["recruiter", "made up"]}
    fake = FakeLLM(lambda p: reply)
    llm = LLMClient("onboarding", provider="mistral", transport=fake.transport(), sleep=False, ledger_dir=tmp_path)
    out = draft_pack("recruiters at agencies", name="agency", client=llm)
    assert out["pack"]["allow_disciplines"] == ["recruiter"]
    assert "Recruiter" in out["keep_ok"] and out["ok"]
    assert '"recruiter"' in fake.payloads[0]["messages"][0]["content"]          # the model sees the blocked list
    assert "recruiter" in blocked_disciplines()
