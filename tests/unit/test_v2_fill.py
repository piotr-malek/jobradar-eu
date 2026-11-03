"""Auto-fill: extension/filler.js on local HTML forms (Playwright is only the test harness here), the payload and
report the app exchanges with the extension, and the unpacked extension end to end. Nothing ever submits."""

from __future__ import annotations

import base64
import http.server
import json
import threading
from pathlib import Path

import pytest

from jobradar.apply import fill

EXT = Path(__file__).parents[2] / "src" / "jobradar" / "extension"
FILLER = (EXT / "filler.js").read_text()

FORM = """<!doctype html><html><body>
<form id="f" action="javascript:void(0)" onsubmit="window.submitted = true; return false;">
  <label for="first_name">First Name</label><input id="first_name" name="first_name">
  <label>Email <input type="email" name="email"></label>
  <label for="why">Why do you want to work at Acme?</label><textarea id="why" name="cards[x][field0]"></textarea>
  <label for="visa">Visa sponsorship?</label><select id="visa" name="visa"><option>Yes</option><option>No</option></select>
  <fieldset><legend>Languages</legend>
    <label><input type="checkbox" name="langs" value="en"> English</label>
    <label><input type="checkbox" name="langs" value="es"> Spanish</label></fieldset>
  <label><input type="checkbox" name="gdpr"> I agree to the privacy policy</label>
  <label>Relocation?</label><div role="radiogroup">
    <div><button type="button" role="radio" aria-checked="false" aria-label="Tak/ Yes" class="rx"></button><input type="radio" name="custom_1" value="0" aria-hidden="true" style="position:absolute;pointer-events:none;opacity:0"></div>
    <div><button type="button" role="radio" aria-checked="false" aria-label="Nie/ No" class="rx"></button><input type="radio" name="custom_1" value="1" aria-hidden="true" style="position:absolute;pointer-events:none;opacity:0"></div></div>
  <div><button type="button" role="checkbox" aria-checked="false" aria-label="I consent to processing the submitted CV" class="rx"></button><input type="checkbox" name="3021" aria-hidden="true" style="position:absolute;pointer-events:none;opacity:0"></div>
  <label for="cv">Resume</label><input type="file" id="cv" name="resume">
  <label for="cl">Cover Letter</label><input type="file" id="cl" name="cover_letter">
  <label for="city">Location</label><input id="city" name="city" role="combobox" aria-expanded="false">
  <label>Start <span><button aria-pressed="false">Yes</button><button aria-pressed="false">No</button>
    <input type="checkbox" name="start_now" style="display:none"></span></label>
  <button type="button" id="apply-btn" onclick="window.applyClicked = true">Apply now</button>
  <button id="plain">Next</button>
  <input type="submit" value="Submit application">
</form>
<script>
  window.enterInText = false;
  document.querySelectorAll('button.rx').forEach(b => b.addEventListener('click', () => {
    if (b.getAttribute('role') === 'radio')
      b.closest('[role=radiogroup]').querySelectorAll('button').forEach(x => x.setAttribute('aria-checked', 'false'));
    b.setAttribute('aria-checked', 'true');
  }));
  document.addEventListener('keydown', e => {
    if (e.key === 'Enter' && ['INPUT', 'TEXTAREA'].includes(e.target.tagName)
        && e.target.getAttribute('role') !== 'combobox') window.enterInText = true;
  }, true);
</script></body></html>"""

PDF = {"name": "cv.pdf", "mime": "application/pdf", "b64": base64.b64encode(b"%PDF-1.4 test").decode()}
LETTER = {"name": "Ana_Ferreira_Cover_Letter.pdf", "mime": "application/pdf",
          "b64": base64.b64encode(b"%PDF-1.4 letter").decode()}
ITEMS = [
    {"key": "first_name", "label": "First Name", "status": "auto", "answer": "Ana", "option": "", "required": True},
    {"key": "email", "label": "Email", "status": "auto", "answer": "p@example.com", "option": "", "required": True},
    {"key": "cards[x][field0]", "label": "Why do you want to work at Acme?", "status": "adjust",
     "answer": "Because of the data team.", "option": "", "required": True},
    {"key": "visa", "label": "Visa sponsorship?", "status": "auto", "answer": "No", "option": "No", "required": True},
    {"key": "langs", "label": "Languages", "status": "once", "answer": "English", "option": "English", "required": False},
    {"key": "gdpr", "label": "I agree to the privacy policy", "status": "auto", "answer": "Yes", "option": "Yes",
     "required": True},
    {"key": "custom_1", "label": "Relocation?", "status": "auto", "answer": "No", "option": "Nie/ No", "required": True},
    {"key": "3021", "label": "I consent to processing the submitted CV", "status": "once", "answer": "Yes", "option": "",
     "required": False},
    {"key": "resume", "label": "Resume", "type": "file", "status": "file", "answer": "", "option": "", "required": True,
     "file": PDF},
    {"key": "cover_letter", "label": "Cover Letter", "type": "file", "status": "adjust", "answer": "Hi Acme team",
     "option": "", "required": False, "file": LETTER},
    {"key": "city", "label": "Location", "status": "auto", "answer": "Rzeszów, Poland", "option": "", "required": True},
    {"key": "start_now", "label": "Start", "type": "boolean", "status": "auto", "answer": "No", "option": "No",
     "required": True},
]

# Ashby: [data-field-path] wrappers, Yes/No buttons over a hidden checkbox, hidden radios with labels, a location
# combobox whose options appear after typing. Lever: location typeahead that searches on keydown and clears itself
# on blur while its list is open. Traffit: selectize (fake with the same API) and a checkbox hidden under its label.
WIDGETS = """<!doctype html><html><body style="height:3000px">
<div data-field-path="_systemfield_name"><label for="_systemfield_name">Full Name</label>
  <input id="_systemfield_name" name="_systemfield_name"></div>
<div data-field-path="_systemfield_location"><label for="_systemfield_location">Location</label>
  <input id="loc" role="combobox" aria-autocomplete="list" aria-controls="lb"><div role="listbox" id="lb"></div></div>
<div data-field-path="visa"><label for="visa">Visa?</label><div>
  <button aria-pressed="false" data-option="yes">Yes</button><button aria-pressed="false" data-option="no">No</button>
  <input type="checkbox" name="visa" style="display:none"></div></div>
<div data-field-path="sf"><fieldset><label for="sf">Salesforce?</label>
  <span><input type="radio" id="r0" name="form1_sf" style="opacity:0;position:absolute"></span><label for="r0">Yes, a lot</label>
  <span><input type="radio" id="r1" name="form1_sf" style="opacity:0;position:absolute"></span><label for="r1">No, I do not.</label>
</fieldset></div>
<input type="file" id="resume-upload-input" name="resume"><span class="resume-upload-working" style="display:none">…</span>
<input class="location-input" name="location" id="location-input"><input type="hidden" id="selected-location" name="selectedLocation">
<div class="dropdown-container" style="display:none"><div class="dropdown-results"></div></div>
<input name="org" id="org"><input type="number" name="years" id="years"><input type="date" name="start" id="start">
<select id="country" name="country" style="display:none"></select>
<select id="langs" name="langs[]" multiple style="display:none"></select>
<div class="custom-control"><input type="checkbox" id="consent" name="consent[8]" style="opacity:0;position:absolute">
  <label for="consent" style="position:relative;z-index:2">Chcę wziąć udział w rekrutacji!</label></div>
<script>
  window.scrolls = 0; addEventListener('scroll', () => window.scrolls++);
  document.querySelectorAll('[aria-pressed]').forEach(b => b.addEventListener('click', () => {
    b.parentElement.querySelectorAll('[aria-pressed]').forEach(x => x.setAttribute('aria-pressed', 'false'));
    b.setAttribute('aria-pressed', 'true');
  }));
  const loc = document.getElementById('loc'), lb = document.getElementById('lb');
  loc.addEventListener('input', () => setTimeout(() => {
    lb.innerHTML = '';
    if (!loc.value.startsWith('Rzesz')) return;
    for (const t of ['Rzeszow, Brandenburg, Germany', 'Rzeszów, Subcarpathian Voivodeship, Poland']) {
      const o = document.createElement('div'); o.setAttribute('role', 'option'); o.textContent = t;
      o.addEventListener('click', () => { loc.value = t; lb.innerHTML = ''; window.locPicked = t; });
      lb.appendChild(o);
    }
  }, 50));
  document.getElementById('resume-upload-input').addEventListener('change', () => {     // Lever's CV parse
    const spin = document.querySelector('.resume-upload-working');
    spin.style.display = 'inline';
    setTimeout(() => { spin.style.display = 'none'; document.getElementById('selected-location').value = '{"parsed":1}'; }, 300);
  });
  const li = document.getElementById('location-input'), box = document.querySelector('.dropdown-container');
  const results = document.querySelector('.dropdown-results');
  li.addEventListener('input', () => { box.style.display = 'flex'; });
  li.addEventListener('keydown', () => setTimeout(() => {
    results.innerHTML = '<div class="dropdown-location" id="location-0">Rzeszów, Podkarpackie, POL</div>';
  }, 100));
  li.addEventListener('blur', () => {
    if (box.style.display !== 'none') { li.value = ''; document.getElementById('selected-location').value = ''; }
  });
  document.addEventListener('mousedown', e => {
    if (!e.target.classList.contains('dropdown-location')) return;
    box.style.display = 'none'; li.value = e.target.textContent;
    document.getElementById('selected-location').value = JSON.stringify({name: e.target.textContent});
    results.innerHTML = '';
  });
  function fakeSelectize(sel, valueField, labelField, options, remote) {
    sel.selectize = {settings: {valueField, labelField, load: remote ? () => {} : null}, options: {}, items: [],
      onSearchChange(q) { if (remote) setTimeout(() => remote(q).forEach(o => { this.options[o[valueField]] = o; }), 100); },
      setValue(v) { this.items = [v]; }, addItem(v) { this.items.push(v); }, close() {}};
    options.forEach(o => { sel.selectize.options[o[valueField]] = o; });
  }
  fakeSelectize(document.getElementById('country'), 'iso', 'country',
                [{iso: 'ua', country: 'Ukraine'}], q => q === 'Poland' ? [{iso: 'pl', country: 'Polska'}] : []);
  fakeSelectize(document.getElementById('langs'), 'value', 'text',
                [{value: '1', text: 'English'}, {value: '2', text: 'Polish'}, {value: '3', text: 'Spanish'}], null);
</script></body></html>"""

WIDGET_ITEMS = [
    {"key": "_systemfield_name", "label": "Full Name", "status": "auto", "answer": "Ana", "option": ""},
    {"key": "_systemfield_location", "label": "Location", "status": "adjust", "answer": "Rzeszów, Poland.", "option": ""},
    {"key": "visa", "label": "Visa?", "type": "boolean", "status": "auto", "answer": "No", "option": "No"},
    {"key": "sf", "label": "Salesforce?", "type": "select", "status": "once", "answer": "x", "option": "No, I do not."},
    {"key": "resume", "label": "Resume/CV", "type": "file", "status": "file", "answer": "", "option": "", "file": PDF},
    {"key": "location", "label": "Current location", "status": "auto", "answer": "Rzeszów, Poland", "option": ""},
    {"key": "org", "label": "Current company", "status": "auto", "answer": "Tidewater Labs", "option": ""},
    {"key": "years", "label": "Years", "status": "auto", "answer": "8", "option": ""},
    {"key": "start", "label": "Start", "status": "auto", "answer": "2026-12-01", "option": ""},
    {"key": "country", "label": "Location (country)", "status": "auto", "answer": "Poland", "option": ""},
    {"key": "langs[]", "label": "Languages", "type": "select", "status": "auto", "answer": "x", "option": "English;Polish"},
    {"key": "consent[8]", "label": "Chcę wziąć udział w rekrutacji!", "type": "boolean", "status": "auto",
     "answer": "Yes", "option": "Yes"},
]


@pytest.fixture(scope="module")
def pw():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:          # one per thread: the extension test shares it
        yield p


@pytest.fixture(scope="module")
def browser(pw):
    try:
        b = pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Chromium for Playwright is not installed ({type(exc).__name__})")
    yield b
    b.close()


def _page(browser, html):
    page = browser.new_page()
    page.set_content(html)
    page.add_script_tag(content=FILLER)
    return page


def _fill(page, items, **opts):
    return page.evaluate("([p, o]) => jobRadarFill(p, o)", [{"items": items}, {"waitMs": 2000, "listMs": 600, "recheckMs": 200, **opts}])


def test_fills_local_form_and_never_submits(browser):
    page = _page(browser, FORM)
    res = _fill(page, ITEMS + [{"key": "missing_q", "label": "Salary expectations", "status": "auto", "answer": "x"}])
    state = page.evaluate("""() => ({
        submitted: !!window.submitted, applyClicked: !!window.applyClicked, enter: window.enterInText,
        first: document.querySelector('#first_name').value, why: document.querySelector('#why').value,
        visa: document.querySelector('#visa').value,
        en: document.querySelector('input[value=en]').checked, es: document.querySelector('input[value=es]').checked,
        gdpr: document.querySelector('input[name=gdpr]').checked,
        cv: document.querySelector('#cv').files[0].name, letter: document.querySelector('#cl').files[0].name,
        radix: [...document.querySelectorAll('button.rx[aria-checked=true]')].map(b => b.getAttribute('aria-label')),
        start: [...document.querySelectorAll('[aria-pressed=true]')].map(b => b.innerText)})""")
    assert state == {"submitted": False, "applyClicked": False, "enter": False, "first": "Ana",
                     "why": "Because of the data team.", "visa": "No", "en": True, "es": False, "gdpr": True,
                     "cv": "cv.pdf", "letter": "Ana_Ferreira_Cover_Letter.pdf",
                     "radix": ["Nie/ No", "I consent to processing the submitted CV"], "start": []}
    by = {r["key"]: r for r in res["report"]}
    assert all(by[k]["ok"] for k in ("first_name", "email", "cards[x][field0]", "visa", "langs", "gdpr", "custom_1",
                                     "3021", "resume", "cover_letter"))
    assert by["missing_q"]["how"] == "field not found"
    assert not by["city"]["ok"] and "pick it by hand" in by["city"]["how"]     # no list appeared: no Enter either
    # Yes/No buttons without a type inside a <form> would submit it: refused, left for the user
    assert not by["start_now"]["ok"] and "submit-like" in by["start_now"]["how"]
    assert res["found"] == 12


def test_is_submit_like(browser):
    page = _page(browser, """<form><input type=submit id=a><button id=b>Next</button>
      <button type=button id=c>Wyślij zgłoszenie</button><button type=button id=e>Yes</button><input id=f value=apply>
      <button type="" id=g>Go</button><button type=bogus id=h>Go</button>
      <button type=button role=checkbox id=i>I consent to processing ... the submitted CV</button>
      <button type=submit role=radio id=j>Yes</button></form><a id=d href=#>Apply for this job</a>
      <button id=k>Yes</button>""")
    got = page.evaluate("""() => { const {isSubmitLike} = jobRadarFill(null, {internals: true});
        return Object.fromEntries([...'abcdefghijk'].map(id => [id, isSubmitLike(document.getElementById(id))])); }""")
    assert got == {"a": True, "b": True, "c": True, "d": True, "e": False, "f": False, "g": True, "h": True,
                   "i": False, "j": True, "k": False}       # k: a button outside any form submits nothing


def test_best_option_folds_accents_and_matches_city_with_region(browser):
    page = _page(browser, "<p>")
    best = lambda opts, v: page.evaluate("([o, v]) => jobRadarFill(null, {internals: true}).bestOption(o, v)", [opts, v])
    locs = ["Rzeszow, Brandenburg, Germany", "Rzeszow, Subcarpathian Voivodeship, Poland"]
    assert best(locs, "Rzeszów, Poland") == 1
    assert best(["Yes", "No", "Decline To Self Identify"], "decline to self identify") == 2
    assert best(["Bachelor's Degree", "Master's Degree"], "Bachelor's Degree") == 0
    assert best(["Aalto University"], "Rzeszow University of Technology") == -1
    assert best(["Rzeszowek", "Rzeszów, Podkarpackie, POL"], "Rzeszów, Poland") == 1            # Lever's country codes
    assert best(["Rzeszowek", "Rżeszów", "Rzeszów"], "Rzeszow, Poland") == 2                    # city alone (Traffit)
    assert best(["Ukraine", "Polska"], "Poland") == 1                                         # Traffit's Polish names
    assert best(["None of the above", "No"], "No") == 1 and best(["None of the above"], "No") == -1


def test_widgets_ashby_lever_selectize_without_scrolling(browser):
    page = _page(browser, WIDGETS)
    res = _fill(page, WIDGET_ITEMS)
    by = {r["key"]: r for r in res["report"]}
    assert [k for k, r in by.items() if not r["ok"]] == [], by
    state = page.evaluate("""() => ({
        name: document.getElementById('_systemfield_name').value, loc: window.locPicked,
        visa: [...document.querySelectorAll('[aria-pressed=true]')].map(b => b.innerText),
        sf: document.getElementById('r1').checked && !document.getElementById('r0').checked,
        lever: document.getElementById('location-input').value, selected: document.getElementById('selected-location').value,
        org: document.getElementById('org').value,
        country: document.getElementById('country').selectize.items, langs: document.getElementById('langs').selectize.items,
        consent: document.getElementById('consent').checked, scrolls: window.scrolls, y: scrollY})""")
    assert state == {"name": "Ana", "loc": "Rzeszów, Subcarpathian Voivodeship, Poland", "visa": ["No"], "sf": True,
                     "lever": "Rzeszów, Podkarpackie, POL", "selected": '{"name":"Rzeszów, Podkarpackie, POL"}',
                     "org": "Tidewater Labs", "country": ["pl"], "langs": ["1", "2"], "consent": True, "scrolls": 0, "y": 0}
    assert by["visa"]["how"] == "path: yes/no" and by["country"]["how"] == "name: select (selectize)"
    rejected = _fill(page, [{"key": "years", "label": "Years", "status": "auto", "answer": "8 years", "option": ""},
                            {"key": "start", "label": "Start", "status": "auto", "answer": "from January", "option": ""}])
    assert [r["how"] for r in rejected["report"]] == ["this number field didn't take the answer: type it by hand",
                                                     "this date field didn't take the answer: type it by hand"]


# Greenhouse's job board: the S3 upload fields arrive after the form shows; a file set before them is not uploaded
# ("Cannot read properties of undefined (reading 'uploadFile')"). After a good upload the input becomes the file name.
GREENHOUSE_UPLOAD = """<!doctype html><html><body>
<div id="box"><button type="button">Attach</button><label for="resume">Resume/CV</label><input id="resume" type="file"></div>
<p id="err"></p>
<script>
window.ENV = {JBEN_URL: "https://boards.greenhouse.io"};
const uploaders = {};
if (!window.neverReady) setTimeout(() => fetch("https://boards.greenhouse.io/uncacheable_attributes/presigned_fields?fields[]=resume")
  .then(r => r.json()).then(() => { uploaders.resume = {uploadFile: async () => {}}; }), 800);
document.getElementById("resume").addEventListener("change", async e => {
  const f = e.target.files[0];
  try { await uploaders.resume.uploadFile(f); } catch (err) { document.getElementById("err").textContent = err.message; return; }
  document.getElementById("box").innerHTML = `<span>${f.name}</span>`;
});
</script></body></html>"""


def test_greenhouse_upload_waits_for_its_upload_fields(browser):
    resume = [{"key": "resume", "label": "Resume/CV", "type": "file", "status": "file", "file": PDF}]
    for never in (False, True):
        page = browser.new_page()
        page.route("**/presigned_fields*", lambda route: route.fulfill(json={"url": "https://s3", "resume": {}}))
        page.set_content(GREENHOUSE_UPLOAD.replace("<script>", "<script>window.neverReady = true;") if never
                         else GREENHOUSE_UPLOAD)
        page.add_script_tag(content=FILLER)
        row = _fill(page, resume, uploadMs=1500)["report"][0]
        if never:                                  # reported for a hand upload, not counted as filled
            assert not row["ok"] and row["how"] == "Greenhouse didn't take the file: attach it by hand"
        else:
            assert row["ok"], row
            assert page.inner_text("#box") == "cv.pdf" and page.inner_text("#err") == ""
        page.close()


# Greenhouse's job board is Remix: the server sends the form, and ~150 ms after load React takes it over and draws each
# input from its own (empty) state. Here "hydration" comes 500 ms after the page shows, as React would: it claims the
# inputs (__reactFiber$) and wipes what was typed before. React's state then follows input events.
SSR_REACT = """<!doctype html><html><body><form>
<label for="first_name">First Name</label><input id="first_name" value="">
<label for="last_name">Last Name</label><input id="last_name" value="">
</form><script>
window.__remixContext = {};
setTimeout(() => {
  for (const el of document.querySelectorAll("input")) { el.__reactFiber$x = {}; el.value = ""; }
}, 500);
</script></body></html>"""

# A page that clears a text field after it was set (a late re-render); "stubborn" keeps clearing it.
CLEARS_ONCE = """<!doctype html><html><body><label for="a">First Name</label><input id="a">
<label for="b">Last Name</label><input id="b"><script>
let n = 0;
document.getElementById("a").addEventListener("input", e => {
  if (n++ === 0 || window.stubborn) setTimeout(() => { e.target.value = ""; }, 50);
});
</script></body></html>"""

NAMES = [{"key": "first_name", "label": "First Name", "status": "auto", "answer": "Ana", "option": ""},
         {"key": "last_name", "label": "Last Name", "status": "auto", "answer": "Ferreira", "option": ""}]


def test_waits_for_react_to_take_over_a_server_rendered_form(browser):
    page = _page(browser, SSR_REACT)
    res = _fill(page, NAMES, recheckMs=50)
    assert [r["ok"] for r in res["report"]] == [True, True]
    page.wait_for_timeout(700)                     # well past the "hydration"
    assert page.evaluate("() => [first_name.value, last_name.value]") == ["Ana", "Ferreira"]
    assert not any("set again" in r["how"] for r in res["report"])      # filled after hydration, not patched up


def test_puts_back_a_text_answer_the_page_cleared(browser):
    page = _page(browser, CLEARS_ONCE)
    items = [dict(NAMES[0], key="a"), dict(NAMES[1], key="b")]
    rows = _fill(page, items)["report"]
    assert page.evaluate("() => [a.value, b.value]") == ["Ana", "Ferreira"]
    assert rows[0]["ok"] and rows[0]["how"].endswith("(set again: the page cleared it)")
    assert rows[1]["how"] == "label: text" or rows[1]["how"] == "id: text"
    page = _page(browser, CLEARS_ONCE.replace("<script>", "<script>window.stubborn = true;"))
    rows = _fill(page, items)["report"]
    assert not rows[0]["ok"] and rows[0]["how"] == "the page cleared this field: type it by hand"
    assert rows[1]["ok"]


def test_payload_and_report(tmp_path):
    cv = tmp_path / "Ana_Ferreira_CV.pdf"
    cv.write_bytes(b"%PDF cv")
    letter = tmp_path / "Ana_Ferreira_Cover_Letter.pdf"
    letter.write_bytes(b"%PDF letter")
    prep = {"job_id": "JR-1", "apply_url": "https://jobs.lever.co/x/1/apply", "cv": str(cv), "items": [
        {"key": "name", "label": "Full name", "type": "text", "status": "auto", "answer": "Ana", "option": "",
         "required": True, "answer_id": "full_name", "mapped_by_ai": True},
        {"key": "resume", "label": "Resume", "type": "file", "status": "file", "answer": "", "required": True},
        {"key": "cl", "label": "Cover letter", "type": "file", "status": "adjust", "answer": "Dear team", "required": False},
        {"key": "salary", "label": "Salary", "type": "text", "status": "missing", "answer": "", "required": True},
        {"key": "pronouns", "label": "Pronouns", "type": "text", "status": "skip", "answer": "", "required": False}]}
    asked = []
    out = fill.payload(prep, lambda text: asked.append(text) or letter)
    assert out["job_id"] == "JR-1" and out["url"] == prep["apply_url"] and asked == ["Dear team"]
    assert [i["key"] for i in out["items"]] == ["name", "resume", "cl"]           # only what the filler may fill
    assert set(out["items"][0]) == set(fill.FIELDS)                               # no stray prep fields
    assert out["items"][1]["file"]["name"] == "Ana_Ferreira_CV.pdf"
    assert base64.b64decode(out["items"][2]["file"]["b64"]) == b"%PDF letter"
    json.dumps(out)
    no_cv = fill.payload({**prep, "cv": str(tmp_path / "gone.pdf")}, lambda text: (_ for _ in ()).throw(OSError()))
    assert no_cv["items"][1]["file"] is None and no_cv["items"][2]["file"] is None
    rows = fill.report(prep, [{"key": "name", "ok": True, "how": "name: text"}, {"key": "cl", "ok": "yes"}, "junk"])
    assert rows == [{"key": "name", "label": "Full name", "ok": True, "how": "name: text"},
                    {"key": "resume", "label": "Resume", "ok": False, "how": "not reported"},
                    {"key": "cl", "label": "Cover letter", "ok": False, "how": "not reported"},
                    {"key": "salary", "label": "Salary", "ok": False, "how": "status missing"}]


class _Pages(http.server.BaseHTTPRequestHandler):
    """/app: a stand-in for the app's job page (carries the jr-app marker); /form: the application form;
    /careers: a careers page embedding the form in a cross-origin iframe (localhost vs 127.0.0.1)."""

    def do_GET(self):  # noqa: N802
        port = self.server.server_address[1]
        body = {"/app": '<!doctype html><meta name="jr-app" content="1"><p>app</p>', "/form": FORM,
                "/careers": f'<!doctype html><h1>Careers</h1><iframe src="http://localhost:{port}/form" width=800 height=900>'
                            '</iframe>'}.get(self.path)
        self.send_response(200 if body else 404)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((body or "").encode())

    def log_message(self, *args):
        pass


def test_extension_end_to_end(pw, tmp_path):
    """The unpacked extension in Chromium: the app page posts a payload, the extension opens the form in a new tab,
    fills it there and replies with the report. Uses the bundled Chromium (Google Chrome no longer loads
    extensions from the command line)."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Pages)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        try:
            ctx = pw.chromium.launch_persistent_context(
                str(tmp_path / "profile"), headless=True, channel="chromium",
                args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium with extensions unavailable ({type(exc).__name__})")
        try:
            app = ctx.new_page()
            app.goto(f"{base}/app")
            app.wait_for_function("document.documentElement.dataset.jrFiller", timeout=10_000)
            payload = {"job_id": "JR-1", "url": f"{base}/form", "items": ITEMS[:9]}
            res = app.evaluate("""p => new Promise(resolve => {
                addEventListener('message', e => { if (e.data && e.data.jr === 'fill-done') resolve(e.data); });
                postMessage({jr: 'fill', payload: p}, location.origin);
            })""", payload)
            assert res["ok"] and res["job_id"] == "JR-1", res
            assert [r["key"] for r in res["report"] if not r["ok"]] == []
            form = next(p for p in ctx.pages if p.url.endswith("/form"))
            assert form.evaluate("""() => [document.querySelector('#first_name').value, !!window.submitted,
                                           document.querySelector('#cv').files.length]""") == ["Ana", False, 1]
            # a page without the jr-app marker gets no bridge
            form.goto(f"{base}/form")
            assert form.evaluate("document.documentElement.dataset.jrFiller") is None

            # the form inside an iframe: the outer page has no fields, the frame gets filled
            res = app.evaluate("""p => new Promise(resolve => {
                addEventListener('message', e => { if (e.data && e.data.jr === 'fill-done') resolve(e.data); });
                postMessage({jr: 'fill', payload: p}, location.origin);
            })""", {**payload, "url": f"{base}/careers"})
            assert res["ok"] and [r["key"] for r in res["report"] if not r["ok"]] == [], res
            careers = next(p for p in ctx.pages if p.url.endswith("/careers"))
            assert careers.frames[1].evaluate("document.querySelector('#first_name').value") == "Ana"
        finally:
            ctx.close()
    finally:
        server.shutdown()
