"""Golden cases for the deterministic matcher. Add a row whenever a real posting was mis-classified."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jobradar.match.engine import MatchEngine
from jobradar.models import Posting

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
EN = "We are looking for you to join our team and work with the data stack. You will build pipelines with our team and have experience with SQL."
EN_DBT = EN + " Our stack: dbt, BigQuery, Airflow, Looker and Terraform on GCP."
DE = "Wir suchen dich für unser Team. Du hast Erfahrung mit der Datenplattform und bist sicher im Umgang mit SQL. Deine Aufgaben und unsere Kenntnisse für die Arbeit mit uns."
PL = "Szukamy osoby do zespołu. Twoje doświadczenie w pracy z danymi jest dla nas ważne oraz znajomość SQL. Oferujemy pracę w zespole, wymagania: doświadczenie."

# (title, location, workplace, countries, description, defaults, expected_decision, expected_section_or_reason)
CASES = [
    ("Senior Analytics Engineer", "Remote - Europe", None, [], EN_DBT, {}, "match", "top"),
    ("Analytics Engineer", "Wrocław, Poland", "hybrid", [], EN, {}, "match", "home"),
    ("Data Engineer (m/w/d)", "Berlin", None, ["DE"], EN, {}, "match", "elsewhere"),
    ("Inżynier Danych", "Wrocław", None, [], PL, {}, "match", "home"),
    ("BI Developer", "Warszawa", None, [], PL, {"default_country": "PL"}, "drop", "title: never word"),
    ("Data Engineer", "Remote (US only)", "remote", [], EN, {}, "drop", "remote outside EU"),
    ("Data Engineer", "Remote", "remote", [], EN + " You must be authorized to work in the United States.", {}, "drop", "remote: US work authorization"),
    ("Data Engineer", "New York, NY", None, [], EN, {}, "drop", "location outside your work countries"),
    ("Analytics Engineer", "London", None, [], EN, {}, "drop", "location outside your work countries"),
    ("Data Engineer", "Remote, Germany", "remote", [], EN, {}, "match", "elsewhere"),  # residency-restricted
    ("Data Engineer", "Remote - EMEA", "remote", [], EN, {}, "match", "remote"),
    ("Data Engineer", "Remote - CET +/- 2h", "remote", [], EN, {}, "match", "remote"),
    ("Data Engineer", "Remote - LATAM", "remote", [], EN, {}, "drop", "remote region not EU"),
    ("Data Engineer", "Remote USA; Remote USA", None, [], EN, {}, "drop", "remote outside EU (US)"),  # "e u" in "remote usa" was read as EU
    ("Data Engineer", "Remote in the U.S.", "remote", [], EN, {}, "drop", "remote"),
    ("Data Engineer", "Remote-NAMER", "remote", [], EN, {}, "drop", "remote region not EU"),
    ("Data Engineer", "Korea", "remote", [], EN, {}, "drop", "remote outside EU (KR)"),
    ("Data Engineer", "Anywhere in the World, United States of America", "remote", [], EN, {}, "drop", "remote outside EU (US)"),
    ("Data Engineer", "Anywhere in Latin America", "remote", [], EN, {}, "drop", "remote"),
    ("Data Engineer", "Anywhere in the World", "remote", [], EN, {}, "match", "remote"),
    ("Data Engineer", "North America, Europe", "remote", [], EN, {}, "match", "remote"),
    ("Sales Engineer - MENA", "Remote", "remote", [], EN, {}, "drop", "title"),
    ("Data Engineer", "Paris", "onsite", [], EN, {}, "drop", "on-site only"),
    ("Data Engineer", "München", None, [], DE, {}, "drop", "posting language: de"),
    ("Data Engineer", "Amsterdam", None, [], EN + " Fluent Dutch is required.", {}, "drop", "requires language: nl"),
    ("Data Engineer", "Madrid", None, [], EN + " Spanish is a plus.", {}, "match", "elsewhere"),
    ("Data Engineer", "Madrid", None, [], EN + " Fluent Spanish required.", {}, "drop", "requires language: es"),
    ("Data Engineering Manager", "Wrocław", None, [], EN, {}, "drop", "title: people manager"),
    ("Head of Data", "Remote - Europe", "remote", [], EN, {}, "drop", "title: above your level"),
    ("Staff Data Engineer", "Remote - Europe", "remote", [], EN, {}, "drop", "title: above your level"),
    ("Data Engineering Intern", "Wrocław", None, [], EN, {}, "drop", "title: internship"),
    ("Werkstudent Data Engineering", "Berlin", None, [], EN, {}, "drop", "title: internship"),
    ("Data Analyst", "Remote - Europe", "remote", [], EN, {}, "drop", "title: not a target role"),
    ("Software Engineer", "Remote - Europe", "remote", [], EN, {}, "drop", "title: not a target role"),
    ("Data Center Technician", "Dublin", None, [], EN, {}, "drop", "title: other discipline"),
    ("Frontend Developer", "Wrocław", None, [], EN, {}, "drop", "title"),
    ("RevOps Engineer", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Marketing Operations Manager", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("AI Automation Engineer", "Warsaw, Poland", "hybrid", [], EN, {}, "drop", "hybrid outside Wrocław (Warsaw)"),
    ("Developer Advocate", "Remote - Europe", "remote", [], EN_DBT + " dbt and Snowflake data pipelines.", {}, "drop",
     "title: never word (developer advocate)"),     # devrel family off (2026-11-01)
    ("Solutions Engineer", "Remote - Europe", "remote", [], "Sell our accounting software to customers.", {}, "drop", "title: never word"),
    ("Data Specialist", "Gdańsk", None, [], EN, {}, "match", "home"),
    ("Reporting Specialist", "Gdańsk", None, [], EN, {}, "drop", "title: not a target role"),   # review family off
    ("Specjalista ds. inżynierii analitycznej", "Wrocław", None, [], PL, {}, "match", "home"),
    ("Programista SQL", "Wrocław", None, [], PL, {}, "match", "home"),
    ("Integrations Engineer", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Test Automation Engineer", "Remote - Europe", "remote", [], EN, {}, "drop", "title"),
    ("Agentic Integration & MCP Engineer", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Revenue Operations Analyst", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Business Systems Engineer", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Senior Business Systems Engineer - Integrations & AI", "Remote - Europe", "remote", [], EN, {}, "drop", "title: above your level"),
    ("Workato Administrator", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Data & Automation Engineer", "Wrocław", None, [], EN, {}, "match", "home"),
    ("Automation Engineer (m/w/d) 100% Remote – n8n & KI", "Remote - Europe", "remote", [], EN + " You build n8n workflows, call APIs and webhooks.", {}, "match", "remote"),
    ("Automation Engineer", "Wrocław", None, [], EN + " PLC programming in Siemens TIA Portal and SCADA.", {}, "drop", "title"),
    ("Software Engineer - Internal Tools & Integrations", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Software Engineer, Internal Tools", "Remote - Europe", "remote", [], EN, {}, "drop", "title: not a target role"),
    ("SAP Integration Engineer (CPI/BTP)", "Remote - Europe", "remote", [], EN, {}, "drop", "title: never word"),
    ("Senior QA Automation Engineer", "Remote - Europe", "remote", [], EN + " Python tests against our APIs.", {}, "drop", "title"),
    ("Integration & Test Engineer", "Remote - Europe", "remote", [], EN, {}, "drop", "title"),
    ("SAP Master Data Consultant", "Wrocław", None, [], EN, {}, "drop", "title: never word"),
    ("Brand Insights, Measurement Lead", "Remote - Europe", "remote", [], EN + " BigQuery, Looker and Snowflake.", {}, "drop", "title: above your level"),
    ("Data Scientist", "Remote - Europe", "remote", [], EN_DBT, {}, "drop", "title: never word"),
    ("Senior QA Data Engineer", "Remote - Europe", "remote", [], EN, {}, "drop", "title: never word"),
    ("Test Data Engineer", "Wrocław", None, [], EN, {}, "drop", "title: never word"),
    ("Data Quality Engineer", "Wrocław", None, [], EN, {}, "match", "home"),
    ("Junior Data Engineer", "Poznań", None, [], EN, {}, "match", "home"),
    ("Data Engineer", "", None, [], EN, {}, "drop", "location unknown"),
    ("Data Engineer", "", None, [], EN, {"default_workplace": "remote"}, "match", "remote"),
    ("Data Engineer", "Zürich", None, [], EN, {}, "match", "elsewhere"),  # CH = eligible (EEA+CH)
    # titles the test profile skips without reading: seniority, a stack he doesn't know, software-type roles
    ("Senior Data Engineer", "Wrocław", None, [], EN, {}, "drop", "title: above your level"),
    ("Lead Analytics Engineer", "Wrocław", None, [], EN, {}, "drop", "title: above your level"),
    ("Starszy Inżynier Danych", "Wrocław", None, [], PL, {}, "drop", "title: above your level"),
    ("Sr. Marketing Automation Specialist", "Remote - Europe", "remote", [], EN, {}, "drop", "title: above your level"),
    ("Senior GCP Data Engineer", "Wrocław", None, [], EN, {}, "match", "home"),
    ("Mid/Senior Data Engineer", "Wrocław", None, [], EN, {}, "match", "home"),
    ("(Senior) Data Engineer", "Wrocław", None, [], EN, {}, "match", "home"),
    ("Azure Data Engineer", "Wrocław", None, [], EN, {}, "drop", "title: never word"),
    ("Senior Data Engineer (Snowflake)", "Wrocław", None, [], EN, {}, "drop", "title: never word"),
    ("Power BI Developer", "Wrocław", None, [], EN, {}, "drop", "title: never word"),
    ("Big Data Engineer (Scala)", "Wrocław", None, [], EN, {}, "drop", "title: never word"),
    ("Salesforce Developer", "Remote - Europe", "remote", [], EN, {}, "drop", "title: never word"),
    ("Mid/Senior Cloud Data Engineer (GCP / Azure / Snowflake / BigQuery)", "Wrocław", None, [], EN, {}, "match", "top"),
    ("Forward Deployed Engineer", "Remote - Europe", "remote", [], EN, {}, "drop", "title: never word"),
    ("Applied AI Engineer", "Remote - Europe", "remote", [], EN, {}, "drop", "title: never word"),
    ("AI Data Engineer", "Wrocław", None, [], EN, {}, "match", "home"),
    # GTM engineer spellings the gtm patterns missed (2026-10-29)
    ("(Senior) GTM-Engineer (m/w/d)", "Berlin", None, ["DE"], EN, {}, "match", "elsewhere"),
    ("Go-to-Market (GTM) Engineer", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("GTM Technology Engineer", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Technical GTME", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Revenue Systems & Operations Engineer", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Marketing Engineer, Demand Generation", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Growth Operations & Automation Specialist", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Pre-Sales Systems Engineer", "Remote - Europe", "remote", [], EN, {}, "drop", "title: never word"),
    ("Technical Marketing Engineer", "Remote - Europe", "remote", [], EN, {}, "drop", "title: not a target role"),
    # data/analytics engineer spellings the core patterns missed (2026-10-29)
    ("Data Engineers and Architects", "Remote - Europe", "remote", [], EN, {}, "drop", "title: never word"),
    ("Analytic Engineer", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Database-Engineer (m/w/d)", "Berlin", None, ["DE"], EN, {}, "match", "elsewhere"),
    ("Datawarehouse Entwickler (m/w/d)", "Berlin", None, ["DE"], EN, {}, "match", "elsewhere"),
    ("Engenheiro(a) de Dados", "Lisboa, Portugal", None, [], EN, {}, "match", "elsewhere"),
    ("Ingeniero/a de Datos GCP", "Madrid", None, ["ES"], EN, {}, "match", "elsewhere"),
    ("Ingénieur·e Data (H/F)", "Paris", None, ["FR"], EN, {}, "match", "elsewhere"),
    ("Architecte Data (F/H)", "Paris", None, ["FR"], EN, {}, "drop", "title: never word"),
    # Notion clean-up 2026-11-01: devrel and review off, non-GCP architects out, three review titles moved to core
    ("Data Product Engineer, Data & Analytics", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Cloud Data Pipelines Engineer", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Data and ML Engineer (Remote)", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("GCP Data Architect", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Data Architect", "Remote - Europe", "remote", [], EN, {}, "drop", "title: never word"),
    ("Architekt Hurtowni Danych", "Remote - Europe", "remote", [], EN, {}, "drop", "title: never word"),
    ("AI Solutions Architect", "Remote - Europe", "remote", [], EN, {}, "drop", "title: never word"),
    ("Data Governance Specialist", "Remote - Europe", "remote", [], EN, {}, "drop", "title: not a target role"),
    # keep_titles: every Senior Analytics Engineer title stays; staff/lead still drop
    ("Senior BI / Analytics Engineer", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Senior Analytics & Data Engineer", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Analytics Engineer, Senior", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Sr. Analytics Engineer – Commercial Intelligence", "Remote - Europe", "remote", [], EN, {}, "match", "remote"),
    ("Staff Analytics Engineer", "Remote - Europe", "remote", [], EN, {}, "drop", "title: above your level"),
    ("Senior Data Engineer", "Remote - Europe", "remote", [], EN, {}, "drop", "title: above your level"),
    ("Software Engineer - Data Platform", "Remote - Europe", "remote", [], EN_DBT, {}, "drop", "title: not a target role"),
    ("Stage - Ingénieur(e) Data", "Paris", None, ["FR"], EN, {}, "drop", "title: internship/student level"),
    # hybrid only near Wrocław (the test profile won't relocate); remote anywhere; unknown mode or unnamed place kept
    ("Data Engineer GCP", "Warszawa, PL", "hybrid", [], EN, {}, "drop", "hybrid outside Wrocław (Warszawa)"),
    ("Analytics Engineer", "Barcelona, , Spain", "hybrid", [], EN, {}, "drop", "hybrid outside Wrocław"),
    ("Data Engineer", "DE", "hybrid", ["DE"], EN, {}, "drop", "hybrid outside Wrocław (DE)"),
    ("Data Engineer", "Warszawa; Wrocław; Gdańsk", "hybrid", [], EN, {}, "match", "home"),
    ("BI Data Engineer", "Oława, Dolnośląskie", "hybrid", [], EN, {}, "match", "home"),
    ("Data Engineer", "Poland", "hybrid", [], EN, {}, "match", "home"),                  # city not named
    ("Data Engineer", "Warszawa, PL", "remote", [], EN, {}, "match", "remote"),               # "Remote · Warszawa"
    ("AI Automation Engineer", "Warszawa, PL", "hybrid", [], EN + " You can work in a hybrid model, from the office, "
     "or remotely. Remote work is also possible.", {}, "match", "home"),
    ("Revenue Operations Manager", "Hamburg, Germany", None, [],
     EN + " Location: remote, with occasional travel. Hybrid work model at our Hamburg office.", {}, "match", "elsewhere"),
    ("Inżynier Danych", "Poznań", "hybrid", [], PL + " Lokalizacja: zdalnie / Poznań (spotkania raz w miesiącu).",
     {}, "match", "home"),
    ("BI Data Engineer", "Brussels, Belgium", None, [], EN + " Benefits: hybrid working model, meal allowance.", {},
     "drop", "hybrid outside Wrocław (Brussels)"),
    ("BI Data Engineer", "Brussels, Belgium", None, [], EN + " Design BI for cloud, on-premises and hybrid environments.",
     {}, "match", "elsewhere"),                                                              # not the work setup
    ("Data Engineer", "Remote, France", "remote", [], EN, {}, "match", "elsewhere"),         # kept; screen caps fit 4
    ("Data Engineer", "Thuiswerken", None, [], EN, {"default_country": "NL"}, "match", "remote"),   # Jooble NL "Remote"
]


# purpose track (postings tagged "purpose"): (title, tags, expected_decision, expected_reason_prefix)
PURPOSE = [
    ("Information Management Officer", ["grade:P-3"], "match", None),
    ("Information Management Officer", ["grade:P-4"], "drop", "purpose: grade"),
    ("Data Management Associate", ["grade:G-6"], "match", None),
    ("Statistical Assistant", ["grade:G-5"], "drop", "purpose: grade"),
    ("Information Systems Officer, P3", [], "match", None),
    ("Data Engineer", ["grade:AD 9"], "drop", "purpose: grade"),
    ("IT Officer", ["category:NPO"], "drop", "purpose: category"),
    ("Public Information Officer", [], "drop", "title: purpose noise"),
    ("Programme Officer", [], "drop", "title: not a target role"),
    ("Data Officer", [], "drop", "purpose: internal"),
]


@pytest.fixture(scope="module")
def engine() -> MatchEngine:
    return MatchEngine.load()


@pytest.mark.parametrize("title,loc,wp,countries,desc,defaults,decision,expected", CASES, ids=[f"{c[0]}|{c[1]}" for c in CASES])
def test_golden(engine, title, loc, wp, countries, desc, defaults, decision, expected):
    p = Posting(source="t", source_job_id=title + loc, title=title, company="Acme", url="https://x", location_raw=loc,
                workplace=wp, countries=countries, description=desc, posted_at=NOW - timedelta(days=1))
    m = engine.match(p, now=NOW, **defaults)
    assert m.decision == decision, (m.drop_reason, m.explain)
    if decision == "match":
        section = "top" if "top_pick" in m.flags else m.section
        if expected == "review":
            assert m.family == "review"
        else:
            assert section == expected, (m.section, m.flags, m.explain)
    else:
        assert m.drop_reason and m.drop_reason.startswith(expected), m.drop_reason


def test_stale_postings_dropped(engine):
    p = Posting(source="t", source_job_id="old", title="Data Engineer", company="Acme", url="u",
                location_raw="Wrocław", description=EN, posted_at=NOW - timedelta(days=45))
    assert engine.match(p, now=NOW).drop_reason.startswith("stale")


def test_spanish_plus_tag(engine):
    p = Posting(source="t", source_job_id="es", title="Data Engineer", company="Acme", url="u",
                location_raw="Madrid", description=EN + " Spanish is a plus.", posted_at=NOW)
    assert any("Spanish" in f for f in engine.match(p, now=NOW).flags)


def test_rule_version_is_stable(engine):
    assert engine.rule_version == MatchEngine.load().rule_version
    assert engine.rule_version.startswith("v")


@pytest.mark.parametrize("title,tags,decision,expected", PURPOSE, ids=[f"{c[0]}|{c[1]}" for c in PURPOSE])
def test_purpose(engine, title, tags, decision, expected):
    desc = EN + (" This vacancy is open to internal candidates only." if title == "Data Officer" else "")
    p = Posting(source="t", source_job_id=title + str(tags), title=title, company="UN", url="https://x",
                location_raw="Geneva, Switzerland", description=desc, posted_at=NOW - timedelta(days=1),
                tags=["purpose", *tags])
    m = engine.match(p, now=NOW)
    assert m.decision == decision, (m.drop_reason, m.explain)
    if decision == "match":
        assert m.family == "purpose"
    else:
        assert m.drop_reason.startswith(expected), m.drop_reason


def test_pregate_lets_data_context_titles_through(engine):
    assert not engine.wants_detail("Solutions Engineer")      # devrel family off (2026-11-01)
    assert not engine.wants_detail("Data Governance Specialist")   # review family off: no detail fetch either
    assert engine.wants_detail("Automation Engineer")         # integration only with automation tooling in the text
    assert not engine.wants_detail("Software Engineer")
    assert engine.wants_detail("Information Management Officer", purpose=True)
    assert not engine.wants_detail("Information Management Officer")


def _prio(engine, title, desc=EN):
    p = Posting(source="t", source_job_id=title, title=title, company="Acme", url="u", location_raw="Remote - Europe",
                workplace="remote", description=desc, posted_at=NOW - timedelta(days=1))
    m = engine.match(p, now=NOW)
    assert m.decision == "match", m.drop_reason
    return m.priority


def test_title_bonus_ranks_engineer_roles_above_ops_and_admin(engine):
    assert _prio(engine, "GTM Engineer") > _prio(engine, "Revenue Operations Manager") + 10
    assert _prio(engine, "Business Automation Developer") > _prio(engine, "Marketing Automation Specialist")
    assert _prio(engine, "CRM Administrator") < _prio(engine, "CRM Developer")
    assert _prio(engine, "(Senior) GTM-Engineer") == _prio(engine, "GTM Engineer")
    assert _prio(engine, "Go-to-Market (GTM) Engineer") > _prio(engine, "Growth Operations Manager") + 10


def test_gcp_stack_ranks_above_spark_stack(engine):
    gcp = _prio(engine, "Data Engineer", EN + " BigQuery, Cloud Functions and Pub/Sub on GCP.")
    spark = _prio(engine, "Data Engineer", EN + " Spark, Databricks and Snowflake.")
    assert gcp > spark + 20


def test_review_off_still_lets_purpose_titles_outside_the_vocabulary_through(engine):
    """UNICEF "Digital Analytics Implementation Specialist Consultant" (kept 2026-11-01) only ever matched via review."""
    title = "Digital Analytics Implementation Specialist Consultant, PFP, 12 months, Rome - Remote"
    assert engine.title_verdict(title, purpose=True).tier != "excluded"
    assert engine.title_verdict(title).tier == "excluded"
