from pathlib import Path

import pytest

from jobradar.apply.forms import READABLE, FormSpec, Question, TYPES, fetch_form
from jobradar.http.client import PoliteClient, ReplayTransport

FIX = Path(__file__).parents[1] / "fixtures" / "forms"
# vendor -> (slug, job id) recorded on 2026-10-23
JOBS = {
    "greenhouse": ("gitlab", "8860302002"),
    "lever": ("palantir", "6ed76ce8-4156-4b60-b120-403538bd66cd"),
    "ashby": ("watershed", "c78ed9ab-92ff-402d-88de-21220c10b56d"),
    "workable": ("huggingface", "DB4D7C0EC8"),
    "recruitee": ("bunq", "head-of-product-6"),
    "erecruiter": ("", "3e93d81624d2447aae2c4cd9f8f352d3"),     # Alten Polska, recorded 2026-10-24
    "traffit": ("antalpl", "03a8959133a279ebe321584229ca088f77676e584665383d"),    # Antal, recorded 2026-10-26
}


def _form(vendor: str) -> FormSpec:
    slug, job = JOBS[vendor]
    with PoliteClient("apply_prep", transport=ReplayTransport(FIX / vendor), sleep=False, respect_robots=vendor in ("lever", "recruitee", "erecruiter", "traffit")) as http:
        spec = fetch_form(vendor, slug, job, http)
    assert spec is not None
    return spec


def _by_key(spec: FormSpec) -> dict[str, Question]:
    return {q.key: q for q in spec.questions}


@pytest.mark.parametrize("vendor", sorted(JOBS))
def test_common(vendor):
    spec = _form(vendor)
    assert spec.vendor == vendor and spec.apply_url.startswith("https://")
    assert 9 <= len(spec.questions) <= 30
    keys = [q.key for q in spec.questions]
    assert all(keys) and len(set(keys)) == len(keys)
    assert all(q.label and q.type in TYPES for q in spec.questions)
    assert any(q.required for q in spec.questions)
    if vendor != "ashby":  # Watershed's form has no select fields
        assert all(q.options for q in spec.questions if q.type in ("select", "multiselect"))
    assert FormSpec.from_dict(spec.to_dict()) == spec


def test_greenhouse():
    spec = _form("greenhouse")
    q = _by_key(spec)
    assert len(q) == 19 and q["hispanic_ethnicity"].type == "select" and spec.apply_url == "https://job-boards.greenhouse.io/embed/job_app?for=gitlab&token=8860302002"
    assert q["first_name"].required and q["first_name"].type == "text"
    assert q["resume"].type == "file"
    assert any(x.type == "select" and "Yes" in x.options for x in q.values())
    assert any(x.type == "select" and not x.required for x in q.values())  # EEOC compliance questions
    assert spec.content and "<" not in spec.content


def test_lever():
    spec = _form("lever")
    q = _by_key(spec)
    assert len(q) == 21 and spec.apply_url.endswith("/apply")
    assert q["resume"].type == "file" and q["resume"].required
    assert q["email"].type == "email" and q["email"].required
    assert q["phone"].type == "phone" and not q["phone"].required
    assert q["urls[LinkedIn]"].type == "url"
    assert "✱" not in q["name"].label
    langs = next(x for x in q.values() if x.type == "multiselect")
    assert langs.key.startswith("cards[") and langs.key.endswith("[field0]") and "English (ENG)" in langs.options
    assert any(x.type == "select" and x.options == ["Yes", "No"] for x in q.values())
    assert any(x.type == "textarea" and not x.required for x in q.values())


def test_ashby():
    spec = _form("ashby")
    q = _by_key(spec)
    assert len(q) == 9 and spec.apply_url == "https://jobs.ashbyhq.com/watershed/c78ed9ab-92ff-402d-88de-21220c10b56d/application"
    assert any(x.type == "email" and x.required for x in q.values())
    assert any(x.type == "file" for x in q.values())
    assert any(x.key.startswith("_systemfield_") for x in q.values())
    assert spec.content and "<" not in spec.content


def test_workable():
    spec = _form("workable")
    q = _by_key(spec)
    assert len(q) == 17 and spec.apply_url == "https://apply.workable.com/huggingface/j/DB4D7C0EC8/apply/"
    assert q["email"].type == "email" and q["resume"].type == "file" and q["resume"].required
    notice = q["CA_10627"]
    assert notice.type == "select" and "Available immediately" in notice.options
    assert all(not o.isdigit() for x in q.values() for o in x.options)  # labels, not numeric ids
    assert q["CA_10628"].type == "boolean" and q["firstname"].max_length == 127
    assert any(x.type == "textarea" for x in q.values())


def test_recruitee():
    spec = _form("recruitee")
    q = _by_key(spec)
    assert len(q) == 23 and spec.apply_url == "https://careers.bunq.com/o/head-of-product-6/c/new"
    assert q["name"].required and q["email"].type == "email" and q["phone"].required
    assert "cv" not in q  # options_cv is "off" for this offer
    custom = [x for x in q.values() if x.key.startswith("open_question_answers[")]
    assert len(custom) == 20 and "<" not in custom[1].label
    assert spec.content


def test_erecruiter():
    from jobradar.apply.route import route_for_url

    spec = _form("erecruiter")
    q = _by_key(spec)
    assert spec.apply_url == "https://form.erecruiter.pl/form/3e93d81624d2447aae2c4cd9f8f352d3" and spec.content == ""
    assert q["firstName"].label == "Imię" and q["firstName"].required and q["email"].type == "email"
    assert q["phone"].type == "phone" and q["cvFiles"].type == "file" and q["cvFiles"].required
    assert q["country"].type == "text" and not q["country"].required            # Radix select: options load live
    assert q["custom_916302"].options == ["Tak/ Yes", "Nie/ No"] and q["custom_916302"].required
    assert len(q["custom_916304"].options) == 11 and "*" not in q["custom_916304"].label
    assert q["3021"].type == "boolean" and not q["3021"].required and "consent" in q["3021"].label
    r = route_for_url("https://form.erecruiter.pl/form/3E93D81624D2447AAE2C4CD9F8F352D3?utm=x", "pasted")
    assert r.vendor == "erecruiter" and r.job == "3e93d81624d2447aae2c4cd9f8f352d3" and r.readable


def test_traffit():
    from jobradar.apply.route import route_for_members, route_for_url

    spec = _form("traffit")
    q = _by_key(spec)
    assert spec.apply_url == "https://antalpl.traffit.com/public/form/a/03a8959133a279ebe321584229ca088f77676e584665383d"
    assert q["dynamic_form[properties][71163][260261]"].label == "Imię" and q["dynamic_form[properties][71163][260261]"].required
    assert q["dynamic_form[properties][71163][260263]"].type == "email" and q["dynamic_form[properties][71163][260264]"].type == "phone"
    assert q["dynamic_form[properties][71163][260266]"].type == "file" and q["dynamic_form[properties][71163][260266]"].label == "CV"
    assert q["dynamic_form[properties][71163][260267]"].type == "url" and not q["dynamic_form[properties][71163][260267]"].required
    assert q["dynamic_form[properties][71163][260265]"].label == "Twoja lokalizacja (city)"
    assert q["dynamic_form[properties][71164][260258]"].label.startswith("Oczekiwana stawka")
    consent = q["dynamic_form[provisions][8]"]
    assert consent.type == "boolean" and consent.required and consent.label == "Chcę wziąć udział w rekrutacji!"
    assert not q["dynamic_form[provisions][7]"].required
    r = route_for_url("https://antalpl.traffit.com/public/form/a/03A8959133a279ebe321584229ca088f77676e584665383d?source=justjoin.it", "pasted")
    assert (r.vendor, r.slug, r.job, r.readable) == ("traffit", "antalpl", "03a8959133a279ebe321584229ca088f77676e584665383d", True)
    assert route_for_url("https://antalpl.traffit.com/public/an/b437a2cf596dfee6c0785fe14ddd431c656a464c614b633d") is None
    member = {"ats_type": "traffit", "ats_slug": "antalpl", "ats_job_id": "18058", "source": "ats",  # board feed: advert id
              "apply_url": "https://antalpl.traffit.com/public/form/a/12339e824b8196a0a10235e4e2a9459b533839715446413d"}
    assert route_for_members([member]).job == "12339e824b8196a0a10235e4e2a9459b533839715446413d"


def test_traffit_radio_group_is_one_choice_question():
    """Antal form recorded 2026-10-30: each radio's label is an option, the question is the group's own label."""
    with PoliteClient("apply_prep", transport=ReplayTransport(FIX / "traffit"), sleep=False, respect_robots=True) as http:
        spec = fetch_form("traffit", "antalpl", "56bdfe366b19c2a1a69ba18a3a0cac5e4d4933645868493d", http)
    q = _by_key(spec)["dynamic_form[properties][71506][261602]"]
    assert (q.label, q.type, q.required) == ("Notice period / Availability", "select", True)
    assert q.options == ["3 months", "1 month", "2 weeks", "1 week", "Immediately", "Other"]
    assert not any(x.label == "3 months" for x in spec.questions)


def test_unknown_vendor_and_readable():
    with PoliteClient("apply_prep", transport=ReplayTransport(FIX / "ashby"), sleep=False) as http:
        assert fetch_form("smartrecruiters", "x", "1", http) is None
        assert http.stats.requests == 0
    assert READABLE == set(JOBS)


def test_missing_job_is_none():
    with PoliteClient("apply_prep", transport=ReplayTransport(FIX / "workable"), sleep=False) as http:
        assert fetch_form("workable", "huggingface", "NOPE", http) is None  # unrecorded -> 404 -> None
