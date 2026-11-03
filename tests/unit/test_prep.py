"""v2 P2: answer bank matching (rules, aliases, LLM mapping, options, PII), prep on recorded forms."""

from __future__ import annotations

import sys

import json
import re
from pathlib import Path

import httpx
import pytest

from fakes import FakeLLM
from jobradar.apply.bank import Answer, Bank, match_form, norm, pick_option
from jobradar.apply.forms import FormSpec, Question
from jobradar.apply.prep import (DRAFT_SYSTEM, PrepHttp, ai_sounding, apply_prep_writes, job_from_row, overview,
                                  prep_job)
from jobradar.apply.profile import background_text, experience
from jobradar.http.client import ReplayTransport
from jobradar.llm.client import LLMClient
from jobradar.local.db import LocalDB

FORMS = Path(__file__).parents[1] / "fixtures" / "forms"
PII = {"email": "ana@example.com", "phone": "+351 900 000 000", "linkedin": "https://linkedin.com/in/af",
       "github": "https://github.com/af", "full_name": "Ana Test", "first_name": "Ana", "last_name": "Test"}


def _bank(**extra: str) -> Bank:
    answers = [Answer(k, "fact", k, v, status="Approved") for k, v in PII.items()]
    answers += [
        Answer("country", "fact", "Country of residence", "Poland", status="Approved"),
        Answer("visa_sponsorship", "fact", "Will you now or in the future require visa sponsorship?", "No",
               status="Approved"),
        Answer("worked_here_before", "fact", "Have you worked for {company} before?", "No", status="Approved",
               aliases=["have you previously worked at or consulted for {company}"]),
        Answer("why_company", "company", "Why do you want to work at {company}?", "I like {company}'s {role} team.",
               status="Approved"),
        Answer("proud_project", "story", "Tell us about a project you are proud of", "", status="Draft"),
        Answer("gender", "eeo", "Gender", "Decline To Self Identify", status="Approved"),
    ]
    return Bank(answers)


def _llm(reply, tmp_path) -> tuple[LLMClient, FakeLLM]:
    fake = FakeLLM(reply)
    return LLMClient("prep", provider="ollama", model="m", transport=fake.transport(), sleep=False,
                     ledger_dir=tmp_path), fake


def _form(*qs: Question) -> FormSpec:
    return FormSpec("greenhouse", "https://x/apply", list(qs))


def test_norm_replaces_company_and_punctuation():
    assert norm("Have you previously worked at or consulted for GitLab?*", "GitLab") == \
        "have you previously worked at or consulted for {company}"
    assert norm("  Why  Hugging Face, and…", "Hugging Face") == "why {company} and"


def test_pick_option():
    assert pick_option("No", ["Yes", "No"], "select") == "No"
    assert pick_option("Yes, within the EU", ["Yes", "No"], "select") == "Yes"
    assert pick_option("Yes", [], "boolean") == "Yes"
    assert pick_option("Poland", ["Albania", "Poland", "Portugal"], "select") == "Poland"
    assert pick_option("No", ["No", "Yes, Netherlands Highly Skilled Migrant Visa"], "select") == "No"
    assert pick_option("English (fluent)", ["English (ENG)", "Spanish (SPA)"], "multiselect") is None
    assert pick_option("No", ["Not applicable", "Yes, I do"], "select") is None        # word boundary
    assert pick_option("No", ["Non-binary", "No, I don't"], "select") == "No, I don't"


def test_rules_exact_aliases_statuses_on_greenhouse_fixture(tmp_path):
    from jobradar.apply.forms import fetch_form
    from jobradar.http.client import PoliteClient

    with PoliteClient("t", transport=ReplayTransport(FORMS / "greenhouse"), sleep=False) as h:
        form = fetch_form("greenhouse", "gitlab", "8860302002", h)
    res = match_form(form, _bank(), company="GitLab", role="Analytics Engineer", client=None)
    by = {it["key"]: it for it in res.items}
    assert by["first_name"]["status"] == "auto" and by["first_name"]["answer"] == "Ana"
    assert by["resume"]["status"] == "file" and by["resume_text"]["status"] == "skip"
    assert by["cover_letter"]["status"] == "skip" and by["cover_letter"]["answer_id"] == "cover_letter"   # no draft here
    assert by["cover_letter_text"]["status"] == "skip" and not by["cover_letter_text"]["answer_id"]  # the upload carries it
    li = next(it for it in res.items if it["label"] == "LinkedIn Profile")
    assert li["answer_id"] == "linkedin"
    country = next(it for it in res.items if "country of residence" in it["label"])
    assert country["option"] == "Poland" and country["status"] == "auto"
    worked = next(it for it in res.items if "previously worked" in it["label"])
    assert worked["answer_id"] == "worked_here_before" and worked["option"] == "No"      # alias with {company}
    visa = next(it for it in res.items if "sponsorship" in it["label"])
    assert visa["status"] == "missing"            # wording differs and no LLM → stays open
    assert by["gender"]["option"] == "Decline To Self Identify"
    assert res.llm_calls == 0


def test_llm_mapping_aliases_then_second_form_needs_no_call(tmp_path):
    qs = [Question("q1", "Why do you want to join Acme?", "textarea", True),
          Question("q2", "Do you need a work permit sponsorship now or later?", "select", True, ["Yes", "No"]),
          Question("q3", "If a dashboard breaks on Friday evening, what do you do?", "select", True, ["A", "B"]),
          Question("q4", "What is your favourite SQL window function?", "text", True),
          Question("email", "Email", "email", True)]

    def reply(p):
        content = p["messages"][-1]["content"]
        if "Answer bank" in content:
            assert "ana@example.com" not in content and "+351" not in content   # no answers sent
            return {"items": [
                {"key": "q1", "answer_id": "why_company", "kind": "company", "canonical": "Why {company}?"},
                {"key": "q2", "answer_id": "visa_sponsorship", "kind": "fact", "canonical": "Visa sponsorship"},
                {"key": "q3", "answer_id": "", "kind": "choice", "canonical": "Friday incident"},
                {"key": "q4", "answer_id": "", "kind": "new", "canonical": "Favourite SQL window function"}]}
        raise AssertionError("unexpected call")

    bank = _bank()
    llm, fake = _llm(reply, tmp_path)
    res = match_form(_form(*qs), bank, company="Acme", role="AE", client=llm)
    by = {it["key"]: it for it in res.items}
    assert res.llm_calls == 1
    assert by["q1"]["status"] == "adjust" and by["q1"]["answer"] == "I like Acme's AE team."
    assert by["q2"]["option"] == "No" and by["q2"]["status"] == "auto"
    assert by["q3"]["status"] == "choose" and by["q4"]["status"] == "missing"
    assert ("why_company", "why do you want to join {company}") in res.alias_updates
    assert [a.question for a in res.new_rows] == ["Favourite SQL window function"]
    assert by["email"]["answer"] == PII["email"]
    # same questions at another company: aliases hit, no LLM call
    beta = [Question("q1", "Why do you want to join Beta?", "textarea", True), qs[1], qs[3]]
    res2 = match_form(_form(*beta), bank, company="Beta", role="AE", client=llm)
    assert res2.llm_calls == 0 and len(fake.payloads) == 1
    assert {it["key"]: it["answer_id"] for it in res2.items} == {
        "q1": "why_company", "q2": "visa_sponsorship", "q4": res.new_rows[0].answer_id}


def test_option_llm_never_sees_pii(tmp_path):
    qs = [Question("li", "LinkedIn", "select", True, ["Option A", "Option B"]),
          Question("g", "Gender", "select", True, ["Woman", "Man", "Rather not say"]),
          Question("c", "Where do you live?", "select", True, ["Republic of Poland", "Elsewhere"])]
    bank = _bank()
    bank.add_alias("country", "where do you live")
    llm, fake = _llm(lambda p: {"items": [{"key": "c", "option": "Republic of Poland"}]}, tmp_path)
    res = match_form(_form(*qs), bank, company="Acme", role="AE", client=llm)
    sent = json.dumps(fake.payloads)
    assert all(v not in sent for v in PII.values())
    assert "Decline To Self Identify" not in sent                                # eeo answers stay local
    assert {it["key"]: it["option"] for it in res.items} == {"li": "", "g": "Rather not say", "c": "Republic of Poland"}
    assert res.items[0]["status"] == "choose"


def test_prep_job_on_fixture_and_manual_form(tmp_path, monkeypatch):
    monkeypatch.setattr("jobradar.apply.prep.cv_choice",
                        lambda fam, title="": {"variant": "core", "path": "/cv/core.pdf", "why": "default"})
    llm, fake = _llm(lambda p: {"facts": ["Builds ML tools"], "instructions": []} if "posting" in
                     p["messages"][-1]["content"].lower() else {"items": []}, tmp_path)
    job = {"job_id": "JR-AAAAAA", "title": "Analytics Engineer", "company": "GitLab", "family": "core",
           "apply_url": "https://job-boards.greenhouse.io/gitlab/jobs/8860302002", "instructions": ["say hi"]}
    with PrepHttp(transport=ReplayTransport(FORMS / "greenhouse"), sleep=False) as http:
        prep, match = prep_job(job, _bank(), http, llm)
    assert prep["route"]["vendor"] == "greenhouse" and prep["cv"] == "/cv/core.pdf"
    assert prep["apply_url"] == "https://job-boards.greenhouse.io/embed/job_app?for=gitlab&token=8860302002"
    assert prep["warnings"][0].endswith("say hi")
    req_missing = sum(1 for it in prep["items"] if it["required"] and it["status"] == "missing")
    assert prep["effort_min"] == 2 + 4 * req_missing + prep["counts"].get("choose", 0)
    assert prep["prep"] == ("ready" if prep["counts"]["required_open"] == 0 else "needs answers")
    manual, m = prep_job({**job, "apply_url": "https://acme.wd3.myworkdayjobs.com/x/job/Wroclaw/AE_R1"}, _bank(), None, None)
    assert manual["prep"] == "manual form" and m is None and manual["effort_min"] == 12
    assert manual["items"] == []                              # no questions, so no answers


def test_apply_prep_writes_to_local_db(tmp_path):
    db = LocalDB(tmp_path / "local.db")
    db.upsert_answer("why_company", question="Why {company}?", kind="company", answer="x", status="Approved")
    bank = db.bank()
    assert bank.answers["why_company"].approved
    bank.add_alias("why_company", "why join {company}")
    new = Answer("fav_sql", "fact", "Favourite SQL function", aliases=["what is your favourite sql function"])
    from jobradar.apply.bank import MatchResult

    apply_prep_writes(db, bank, {"job_id": "JR-1", "prep": "needs answers", "effort_min": 9, "apply_url": "https://a",
                                 "items": []},
                      MatchResult(items=[], alias_updates=[("why_company", "why join {company}")], new_rows=[new]))
    rows = {r["answer_id"]: r for r in db.bank_rows()}
    assert rows["why_company"]["aliases"] == ["why join {company}"]
    assert rows["fav_sql"]["status"] == "Draft" and rows["fav_sql"]["aliases"] == ["what is your favourite sql function"]
    job = db.job("JR-1")
    assert (job["prep"], job["effort_min"], job["apply_url"]) == ("needs answers", 9, None)   # only one given by hand
    assert db.prep("JR-1")["effort_min"] == 9


def test_job_from_row_prefers_a_url_found_by_hand():
    row = {"job_id": "JR-1", "title": "AE", "company": "Acme", "route_url": "https://boards.greenhouse.io/acme/jobs/1",
           "posting_url": "https://board/x", "instructions": ["Start with BANANA"], "fit": 4}
    job = job_from_row(row)
    assert job["apply_url"].startswith("https://boards.greenhouse.io") and job["instructions"] == ["Start with BANANA"]
    assert job_from_row(row, {"apply_url": "https://jobs.lever.co/acme/1"})["apply_url"] == "https://jobs.lever.co/acme/1"


def test_prep_drafts_company_answers_and_cover_letter_per_job(tmp_path, monkeypatch):
    monkeypatch.setattr("jobradar.apply.prep.cv_choice", lambda fam, title="": {"variant": "", "path": "", "why": ""})
    monkeypatch.setattr("jobradar.apply.prep.background_text", lambda: "- Analytics Engineer, BigQuery, 30+ automations")
    bank = _bank()
    bank.answers["why_company"].status, bank.answers["why_company"].answer = "Draft", "mention their {role} team"
    bank.add(Answer("cover_letter", "story", "Cover letter", "", status="Draft"))
    bank.add(Answer("dbt_experience", "story", "Describe your experience with dbt", "Personal projects only.",
                    status="Approved"))

    def reply(p):
        if p["response_format"]["json_schema"]["name"] == "drafts":
            return {"answers": [{"key": "why", "answer": "Because Acme ships BigQuery tooling."},
                                {"key": "cl", "answer": "Hi Acme team, " + "x" * 400 + "\n\n[Name]"},
                                {"key": "cl_file", "answer": "Hi Acme team, I build data tools."}]}
        return {"facts": ["Acme builds data tools"], "instructions": []}

    llm, fake = _llm(reply, tmp_path)
    form = FormSpec("greenhouse", "https://x/apply", [
        Question("why", "Why do you want to work at Acme?", "textarea", True),
        Question("cl", "Cover letter", "textarea", False, max_length=300),
        Question("country", "Country of residence", "text", True)], "Acme builds data tools for analysts.")
    job = {"job_id": "JR-DRAFTS", "title": "Analytics Engineer", "company": "Acme",
           "apply_url": "https://job-boards.greenhouse.io/acme/jobs/1"}
    prep, match = prep_job(job, bank, None, llm, form=form)
    by = {it["key"]: it for it in prep["items"]}
    assert by["why"]["status"] == "adjust" and by["why"]["drafted"] and "BigQuery" in by["why"]["answer"]
    assert by["cl"]["status"] == "adjust" and by["cl"]["answer"].startswith("Hi Acme")      # optional, still drafted
    assert "[Name]" not in by["cl"]["answer"]
    assert by["country"]["status"] == "auto" and not by["country"].get("drafted")
    assert any("longer than 300" in w for w in prep["warnings"])
    draft_prompt = next(p for p in fake.payloads if p["response_format"]["json_schema"]["name"] == "drafts")
    text = draft_prompt["messages"][-1]["content"]
    assert "mention their Analytics Engineer team" in text and "30+ automations" in text
    assert "Personal projects only." in text and "Acme builds data tools for analysts." in text
    assert text.index("Personal projects only.") < text.index("30+ automations")   # bank stories before the projects
    assert PII["email"] not in text and PII["phone"] not in text
    assert bank.answers["why_company"].answer == "mention their {role} team" and not match.new_rows   # bank untouched

    # a cover-letter upload is drafted too (the app makes the PDF); its "or type it in" twin is left out
    twin = FormSpec("greenhouse", "https://x/apply", [Question("cl_file", "Cover Letter", "file", False),
                                                      Question("cl", "Cover letter", "textarea", False)], "Acme")
    by = {it["key"]: it for it in prep_job(job, bank, None, llm, form=twin)[0]["items"]}
    assert by["cl_file"]["status"] == "adjust" and by["cl_file"]["answer"].startswith("Hi Acme team, I build")
    assert by["cl"]["status"] == "skip" and not by["cl"]["answer"]

    llm2, _ = _llm(lambda p: httpx.Response(400, json={}) if p["response_format"]["json_schema"]["name"] == "drafts"
                   else {"facts": [], "instructions": []}, tmp_path)
    prep2, _ = prep_job(job, bank, None, llm2, form=form)
    assert {it["key"]: it["status"] for it in prep2["items"]}["why"] == "missing"
    assert any("Couldn't draft" in w for w in prep2["warnings"])


def test_background_text_carries_the_project_list(monkeypatch):
    monkeypatch.setattr("jobradar.apply.profile.experience", lambda: "## Main projects\n### Ticket payment service\n")
    text = background_text({"summary": "AE", "strong_stack": ["SQL"], "highlights": ["Bilhete"],
                            "draft_rules": ["Past tense."]})
    assert (text.index("- Bilhete") < text.index("Candidate's projects:") < text.index("Ticket payment service")
            < text.index("Candidate's rules:"))
    monkeypatch.setattr("jobradar.apply.profile.experience", lambda: "")
    assert "Candidate's projects" not in background_text({"summary": "AE"})


def test_experience_file_has_the_tiers_the_draft_prompt_names():
    text = experience()
    assert "## Main projects" in text and "## Small fixes" in text
    assert "Candidate's projects" in DRAFT_SYSTEM and "small fix" in DRAFT_SYSTEM
    assert "\"Candidate's earlier answers\" are a fallback" in DRAFT_SYSTEM
    assert ai_sounding(text) == []          # drafts borrow its wording, so it follows the style guide too


@pytest.mark.parametrize("label,expected", [
    ("What is your motivation for the role and Emma?", True),
    ("Tell us about yourself", True),
    ("Why are you looking to leave your current role?", True),
    ("Describe your experience with dbt", True),
    ("Describe a time a data model ran successfully but produced wrong numbers", False),
    ("Complete the sentence about a fact table you built: it had one row per ___", False),
    ("Tell us about a certified metric or KPI you defined and maintained", False),
    ("Give concrete examples of how you've used AI tools in your analytics work", False),
    ("Describe the largest dbt project you have owned", False),
])
def test_general_questions_get_overviews_story_questions_get_detail(label, expected):
    assert overview({"answer_id": "", "label": label}, _bank()) is expected


def test_cover_letter_and_company_rows_are_always_overviews():
    bank = _bank()
    bank.add(Answer("cover_letter", "story", "Cover letter", "", status="Draft"))
    assert overview({"answer_id": "cover_letter", "label": "Describe a time you led a project"}, bank)
    assert overview({"answer_id": "why_company", "label": "Tell us about a time you used {company}"}, bank)


def test_overview_draft_with_implementation_detail_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr("jobradar.apply.prep.cv_choice", lambda fam, title="": {"variant": "", "path": "", "why": ""})
    monkeypatch.setattr("jobradar.apply.prep.background_text", lambda: "- Analytics Engineer")

    def reply(p):
        if p["response_format"]["json_schema"]["name"] == "drafts":
            return {"answers": [
                {"key": "why", "answer": "I matched clicks to bookings by timing because there was no join key."},
                {"key": "bug", "answer": "Won dates moved a day because of the rep's timezone."}]}
        return {"facts": [], "instructions": []}

    llm, fake = _llm(reply, tmp_path)
    form = FormSpec("greenhouse", "https://x/apply", [
        Question("why", "Why do you want to work at Acme?", "textarea", True),
        Question("bug", "Describe a time a data model gave wrong numbers", "textarea", True)], "Acme")
    prep, _ = prep_job({"job_id": "JR-OV", "title": "AE", "company": "Acme",
                        "apply_url": "https://job-boards.greenhouse.io/acme/jobs/3"}, _bank(), None, llm, form=form)
    by = {it["key"]: it for it in prep["items"]}
    assert by["why"]["warnings"] == ["too detailed for an overview answer: join key"]
    assert not by["bug"].get("warnings")                        # a story question may name the timezone
    assert sum("overview doesn't need" in w for w in prep["warnings"]) == 1
    text = next(p for p in fake.payloads if p["response_format"]["json_schema"]["name"] == "drafts")["messages"][-1]
    assert "Question: Why do you want to work at Acme?\nAnswer type: overview" in text["content"]
    assert "gave wrong numbers\nAnswer type: specific example" in text["content"]


def test_detail_phrases_come_from_their_own_style_section():
    from jobradar.apply.profile import avoid_phrases, detail_phrases
    style = "## Words and phrases to avoid\ndelve, robust\n\n## Too detailed for an overview answer\njoin key,\ngrain\n"
    assert avoid_phrases(style) == ["delve", "robust"] and detail_phrases(style) == ["join key", "grain"]
    assert "join key" in detail_phrases() and "join key" not in avoid_phrases()


def test_job_specific_question_is_drafted_not_banked(tmp_path, monkeypatch):
    monkeypatch.setattr("jobradar.apply.prep.cv_choice", lambda fam, title="": {"variant": "", "path": "", "why": ""})
    monkeypatch.setattr("jobradar.apply.prep.background_text", lambda: "- Analytics Engineer")
    bank = _bank()
    bank.answers["proud_project"].answer, bank.answers["proud_project"].status = "Payment service story.", "Approved"

    def reply(p):
        name = p["response_format"]["json_schema"]["name"]
        if name == "bank_map":
            return {"items": [{"key": "hr", "answer_id": "proud_project", "kind": "job", "canonical": "HR project"}]}
        if name == "drafts":
            return {"answers": [{"key": "hr", "answer": "(1) **Workday** data (2) dbt models (3) cut time by 80%."}]}
        return {"facts": [], "instructions": []}

    llm, fake = _llm(reply, tmp_path)
    form = FormSpec("greenhouse", "https://x/apply", [Question(
        "hr", "Describe a project in three parts: (1) What HR data did you use (2) What did you build (3) Impact",
        "textarea", True)], "HR analytics team on BigQuery.")
    prep, match = prep_job({"job_id": "JR-HR", "title": "AE", "company": "Acme",
                            "apply_url": "https://job-boards.greenhouse.io/acme/jobs/2"}, bank, None, llm, form=form)
    it = prep["items"][0]
    assert it["job_specific"] and it["drafted"] and it["status"] == "adjust" and it["answer"].startswith("(1)")
    assert it["answer_id"] == "" and not match.new_rows and not match.alias_updates      # bank untouched
    assert "**" not in it["answer"]
    assert any(w.endswith("background: 80%") for w in prep["warnings"])   # 80% flagged; (1) (2) (3) are in the question
    assert "Describe a project in three parts" in next(
        p for p in fake.payloads if p["response_format"]["json_schema"]["name"] == "drafts")["messages"][-1]["content"]


@pytest.mark.skipif(sys.platform == "win32", reason="the fake claude CLI is a #! script")
def test_claude_cli_provider_and_draft_purpose(tmp_path, monkeypatch):
    from jobradar.llm.client import optional_client, resolve_provider

    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n"
                    "printf '%s\\n' \"$@\" > \"$(dirname \"$0\")/args\"; cat > \"$(dirname \"$0\")/stdin\"\n"
                    "if [ -n \"$ANTHROPIC_API_KEY\" ]; then echo '{\"is_error\": true, \"api_error_status\": 401}'; exit 1; fi\n"
                    "echo '{\"is_error\": false, \"structured_output\": {\"answers\": [{\"key\": \"q\", \"answer\": \"hi\"}]}}'\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("JR_CLAUDE_BIN", str(fake))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-used")
    monkeypatch.setenv("JR_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("JR_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("JR_PREP_LLM_PROVIDER", "mistral")
    monkeypatch.delenv("JR_DRAFT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("JR_DRAFT_LLM_MODEL", raising=False)
    assert optional_client("draft") is None                                      # no draft settings of its own
    monkeypatch.setenv("JR_DRAFT_LLM_PROVIDER", "claude")
    monkeypatch.setenv("JR_DRAFT_LLM_MODEL", "claude-opus-5-5")
    assert resolve_provider("draft") == ("claude", "claude-opus-5-5")
    with LLMClient("draft", ledger_dir=tmp_path) as llm:
        out = llm.json("Write it.", {"type": "object"}, system="Be honest.")
    assert out == {"answers": [{"key": "q", "answer": "hi"}]}
    args = (tmp_path / "args").read_text(encoding="utf-8").splitlines()
    assert args[:2] == ["-p", "--safe-mode"] and args[args.index("--tools") + 1] == ""
    assert args[args.index("--model") + 1] == "claude-opus-5-5" and "Be honest." in args
    assert (tmp_path / "stdin").read_text(encoding="utf-8") == "Write it."


def test_style_guide_in_draft_prompt_and_ai_words_flagged(tmp_path, monkeypatch):
    from jobradar.apply.prep import ai_sounding

    assert ai_sounding("I leveraged dbt — truly seamless. Navigation is fine.") == ["leveraged", "seamless",
                                                                                    "truly", "—"]
    monkeypatch.setattr("jobradar.apply.prep.cv_choice", lambda fam, title="": {"variant": "", "path": "", "why": ""})
    monkeypatch.setattr("jobradar.apply.prep.background_text", lambda: "- Analytics Engineer")
    llm, fake = _llm(lambda p: {"answers": [{"key": "why", "answer": "I'm passionate about Acme — truly."}]}
                     if p["response_format"]["json_schema"]["name"] == "drafts" else {"facts": [], "instructions": []},
                     tmp_path)
    form = FormSpec("greenhouse", "https://x/apply", [Question("why", "Why do you want to work at Acme?", "textarea",
                                                              True)], "Acme makes tools.")
    prep, _ = prep_job({"job_id": "JR-STYLE", "title": "AE", "company": "Acme",
                        "apply_url": "https://job-boards.greenhouse.io/acme/jobs/3"}, _bank(), None, llm, form=form)
    system = next(p for p in fake.payloads if p["response_format"]["json_schema"]["name"] == "drafts")["messages"][0]
    assert "Words and phrases to avoid" in system["content"] and "Sample answers in the right voice" in system["content"]
    assert any(w.endswith("avoid list: passionate, truly, —") for w in prep["warnings"])


def test_one_field_for_several_profile_links_joins_approved_links():
    res = match_form(_form(Question("p", "Professional Profile (LinkedIn, Github, Website, etc)", "text", True),
                           Question("l", "LinkedIn Profile", "text", False)), _bank(), company="Acme", role="AE")
    by = {it["key"]: it for it in res.items}
    assert by["p"]["status"] == "auto" and by["p"]["answer"] == f"{PII['linkedin']}, {PII['github']}"   # no website
    assert by["l"]["answer_id"] == "linkedin" and not res.new_rows and res.llm_calls == 0


def test_decline_answers_and_greenhouse_education_and_hispanic_fields():
    assert pick_option("I don't wish to answer", ["Yes", "No", "Decline To Self Identify"], "select") == \
        "Decline To Self Identify"
    assert pick_option("Prefer not to say", ["Male", "I prefer not to say"], "select") == "I prefer not to say"
    bank = _bank()
    bank.add(Answer("ethnicity", "eeo", "Race / ethnicity", "I don't wish to answer", status="Approved"))
    bank.add(Answer("education_school", "fact", "School", "University of Lisbon", status="Approved"))
    form = _form(Question("hispanic_ethnicity", "Are you Hispanic/Latino?", "select", False,
                          ["Yes", "No", "Decline To Self Identify"]),
                 Question("school--0", "School", "text", False), Question("degree--0", "Degree", "text", False))
    by = {it["key"]: it for it in match_form(form, bank, company="Acme", role="AE").items}
    assert by["hispanic_ethnicity"]["option"] == "Decline To Self Identify"
    assert by["school--0"]["answer"] == "University of Lisbon" and by["school--0"]["status"] == "auto"
    assert by["degree--0"]["answer_id"] == "education_degree" and by["degree--0"]["status"] == "skip"  # not in bank


def test_missing_free_text_is_drafted_but_never_sensitive_facts(tmp_path, monkeypatch):
    monkeypatch.setattr("jobradar.apply.prep.cv_choice", lambda fam, title="": {"variant": "", "path": "", "why": ""})
    monkeypatch.setattr("jobradar.apply.prep.background_text", lambda: "- Analytics Engineer, dbt in personal projects")

    def reply(p):
        name = p["response_format"]["json_schema"]["name"]
        if name == "drafts":
            keys = set(re.findall(r"### key: (\S+)", p["messages"][-1]["content"]))
            assert keys == {"proud", "start"}               # salary (sensitive) is never sent for drafting
            return {"answers": [{"key": "proud", "answer": "The dbt models for my birding project."},
                                {"key": "start", "answer": ""}]}
        if name == "bank_map":
            return {"items": [{"key": "salary", "answer_id": "salary_expectation", "kind": "fact", "canonical": ""}]}
        return {"facts": [], "instructions": []}

    llm, _ = _llm(reply, tmp_path)
    bank = _bank()
    bank.add(Answer("salary_expectation", "fact", "Salary expectation", "", status="Draft"))
    form = _form(Question("proud", "Tell us about a project you are proud of", "textarea", True),
                 Question("salary", "What are your salary expectations?", "text", True),
                 Question("start", "When could you start?", "text", True))
    prep, _ = prep_job({"job_id": "JR-MISS", "title": "AE", "company": "Acme", "apply_url": "https://x"},
                       bank, None, llm, form=form)
    by = {it["key"]: it for it in prep["items"]}
    assert by["proud"]["status"] == "adjust" and by["proud"]["drafted"] and "birding" in by["proud"]["answer"]
    assert by["salary"]["status"] == "missing" and not by["salary"].get("drafted")
    assert by["start"]["status"] == "missing"                # empty draft: left for the candidate


def test_manual_form_gets_no_answers_and_no_llm_call(tmp_path, monkeypatch):
    """A form we can't read: no drafts, no facts, no LLM call; the app asks for the form's URL or questions."""
    monkeypatch.setattr("jobradar.apply.prep.cv_choice", lambda fam, title="": {"variant": "", "path": "", "why": ""})
    bank = _bank()
    bank.add(Answer("cover_letter", "story", "Cover letter", "", status="Draft"))
    llm, fake = _llm(lambda p: {"answers": [], "facts": ["Consultancy"], "instructions": []}, tmp_path)
    job = {"job_id": "JR-LI", "title": "Senior Data Engineer", "company": "emagine",
           "apply_url": "https://www.linkedin.com/jobs/view/4012345678", "instructions": ["Mention code BLUE"]}
    steps: list[str] = []
    prep, match = prep_job(job, bank, None, llm, posting_text=lambda: "We need dbt and Snowflake.", step=steps.append)
    assert prep["prep"] == "manual form" and match is None and prep["items"] == [] and prep["facts"] == []
    assert prep["counts"] == {"required_open": 0} and prep["route"]["vendor"] == "linkedin"
    assert prep["warnings"][0] == "Posting instruction: Mention code BLUE"   # from Notion, still shown
    assert fake.payloads == [] and steps == ["Finding the application form"]


def test_pasted_questions_are_parsed_matched_and_kept(tmp_path, monkeypatch):
    from jobradar.apply.prep import parse_pasted, pasted_from

    monkeypatch.setattr("jobradar.apply.prep.cv_choice", lambda fam, title="": {"variant": "", "path": "", "why": ""})
    paste = """Contact info
Email address
Required
Will you now or in the future require visa sponsorship?
Select an option
Yes
No
How many years of work experience do you have with dbt?"""
    llm, _ = _llm(lambda p: {"questions": [
        {"label": "Email address", "type": "text", "options": []},
        {"label": "Will you now or in the future require visa sponsorship?", "type": "boolean", "options": ["Yes", "No"]},
        {"label": "How many years of work experience do you have with dbt?", "type": "text", "options": []}]}
        if p["response_format"]["json_schema"]["name"] == "pasted_form" else {"items": []}
        if p["response_format"]["json_schema"]["name"] == "bank_map" else {"answers": []}
        if p["response_format"]["json_schema"]["name"] == "drafts" else {"facts": [], "instructions": []}, tmp_path)
    qs = parse_pasted(paste, llm)
    assert [q.type for q in qs] == ["text", "boolean", "text"] and all(q.required for q in qs)
    assert qs[1].key.startswith("pasted_will_you_now") and len({q.key for q in qs}) == 3
    fallback = parse_pasted(paste, None)                      # no LLM: one question per line, noise dropped
    assert [q.label for q in fallback][:3] == ["Contact info", "Email address",
                                               "Will you now or in the future require visa sponsorship?"]
    job = {"job_id": "JR-LI", "title": "AE", "company": "emagine", "apply_url": "https://www.linkedin.com/jobs/view/1"}
    prep, match = prep_job(job, _bank(), None, llm, pasted=qs)
    by = {it["label"]: it for it in prep["items"]}
    assert prep["prep"] in ("ready", "needs answers") and match is not None
    assert by["Email address"]["answer"] == PII["email"]
    assert by["Will you now or in the future require visa sponsorship?"]["option"] == "No"
    assert [q.label for q in pasted_from(prep)] == [q.label for q in qs]
    assert prep["route"]["vendor"] == "linkedin"              # still opened in the browser, not filled
