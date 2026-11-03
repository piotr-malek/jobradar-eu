"""Read an ATS application form's questions ahead of time (read-only, never submits).

One function per vendor returns a normalised ``FormSpec``; ``fetch_form`` dispatches on the vendor name.
All traffic goes through the caller's ``PoliteClient`` (one or two requests per job the owner chose to apply to).
Lever, Recruitee, eRecruiter and Traffit serve HTML/frontend hosts: build the client with ``respect_robots=True`` (and
``browser=True`` for the HTML pages); a ``Blocked`` there makes ``fetch_form`` return None. ``BudgetExceeded`` propagates.

Question keys (what a filler uses to find the input in the live form):
  greenhouse  field ``name`` (``first_name``, ``resume``, ``question_123``)
  lever       input ``name`` (``name``, ``email``, ``urls[LinkedIn]``, ``resume``) / ``cards[<card id>][field<i>]``
  ashby       field ``path`` (``_systemfield_name``, or the custom field's path)
  workable    field ``id`` (``firstname``, ``resume``, ``QA_123``)
  recruitee   ``name`` / ``email`` / ``phone`` / ``cv`` / ``cover_letter``; custom: ``open_question_answers[<id>]``
  erecruiter  input ``name`` (``firstName``, ``email``, ``cvFiles``, ``custom_916301``, consent ``3021``)
  traffit     input ``name`` (``dynamic_form[properties][<section>][<field>]``, consent ``dynamic_form[provisions][8]``)
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from bs4 import BeautifulSoup

from jobradar.http.client import Blocked, PoliteClient
from jobradar.normalize.text import html_to_text

READABLE = {"greenhouse", "lever", "ashby", "workable", "recruitee", "erecruiter", "traffit"}
TYPES = {"text", "textarea", "select", "multiselect", "boolean", "file", "email", "phone", "url", "date", "number"}


@dataclass
class Question:
    key: str
    label: str
    type: str
    required: bool
    options: list[str] = field(default_factory=list)
    description: str = ""
    max_length: int | None = None


@dataclass
class FormSpec:
    vendor: str
    apply_url: str
    questions: list[Question]
    content: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FormSpec":
        return cls(
            vendor=d["vendor"],
            apply_url=d["apply_url"],
            questions=[Question(**q) for q in d.get("questions", [])],
            content=d.get("content", ""),
        )


def _opts(values: list[Any] | None, key: str) -> list[str]:
    return [str(v[key]).strip() for v in values or [] if isinstance(v, dict) and v.get(key) is not None]


# -- Greenhouse -------------------------------------------------------------------------------------
_GH_TYPES = {
    "input_text": "text",
    "textarea": "textarea",
    "input_file": "file",
    "multi_value_single_select": "select",
    "multi_value_multi_select": "multiselect",
}


def greenhouse_form(http: PoliteClient, slug: str, job_id: str) -> FormSpec | None:
    resp = http.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}?questions=true")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    groups = list(data.get("questions") or []) + list(data.get("location_questions") or [])
    for block in data.get("compliance") or []:
        groups += block.get("questions") or []
    out: list[Question] = []
    for q in groups:
        for f in q.get("fields") or []:
            if f.get("type") == "input_hidden" or not f.get("name"):
                continue
            if f["name"] == "race":           # the live form asks this first; Race only shows after "No"
                out.append(Question("hispanic_ethnicity", "Are you Hispanic/Latino?", "select", False,
                                    ["Yes", "No", "Decline To Self Identify"]))
            out.append(
                Question(
                    key=f["name"],
                    label=(q.get("label") or "").strip(),
                    type=_GH_TYPES.get(f.get("type"), "text"),
                    required=bool(q.get("required")),
                    options=_opts(f.get("values"), "label"),
                    description=html_to_text(q.get("description")),
                )
            )
    if data.get("education") in ("education_optional", "education_required"):
        # not listed as questions: the form builds these widgets itself (typeahead lists + year inputs)
        req = data["education"] == "education_required"
        out += [Question(k, label, "text", req) for k, label in (
            ("school--0", "School"), ("degree--0", "Degree"), ("discipline--0", "Discipline"),
            ("start-year--0", "Start date year"), ("end-year--0", "End date year"))]
    # The embed URL always renders the bare form. absolute_url (and job-boards.greenhouse.io) can redirect to the
    # company's own careers page, where the form sits behind "Apply" in an iframe the filler can't see.
    return FormSpec(
        "greenhouse", f"https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={job_id}", out,
        html_to_text(html_lib.unescape(data.get("content") or ""))
    )


# -- Lever --------------------------------------------------------------------------------------------
_LV_TYPES = {
    "multiple-choice": "select",
    "multiple-select": "multiselect",
    "dropdown": "select",
    "textarea": "textarea",
    "text": "text",
}
_LV_STD_OPTS = {"resume", "cards", "urls"}


def _lever_std_type(name: str, input_type: str) -> str:
    if input_type == "file":
        return "file"
    if name == "email":
        return "email"
    if name == "phone":
        return "phone"
    if name.startswith("urls["):
        return "url"
    return "text"


def lever_form(http: PoliteClient, slug: str, job_id: str) -> FormSpec | None:
    resp = None
    for host in ("jobs.lever.co", "jobs.eu.lever.co"):
        url = f"https://{host}/{slug}/{job_id}/apply"
        resp = http.get(url)
        if resp.status_code != 404:
            break
    if resp is None or resp.status_code == 404:
        return None
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    out: list[Question] = []
    for li in soup.select("li.application-question"):
        label_el = li.select_one(".application-label")
        if "custom-question" in (li.get("class") or []) or not label_el:
            continue
        inp = li.select_one("input:not([type=hidden]), textarea, select")
        if inp is None or not inp.get("name"):
            continue
        text = label_el.get_text(" ", strip=True)
        out.append(
            Question(
                key=inp["name"],
                label=text.replace("✱", "").strip(),
                type=_lever_std_type(inp["name"], inp.get("type") or ""),
                required="✱" in text,
            )
        )
    for tpl in soup.select('input[name$="[baseTemplate]"]'):
        m = re.match(r"cards\[([^\]]+)\]\[baseTemplate\]$", tpl.get("name", ""))
        if not m:
            continue
        try:
            card = json.loads(tpl.get("value") or "{}")
        except ValueError:
            continue
        for i, f in enumerate(card.get("fields") or []):
            out.append(
                Question(
                    key=f"cards[{m.group(1)}][field{i}]",
                    label=(f.get("text") or "").strip(),
                    type=_LV_TYPES.get(f.get("type"), "text"),
                    required=bool(f.get("required")),
                    options=_opts(f.get("options"), "text"),
                    description=html_to_text(f.get("description")),
                )
            )
    return FormSpec("lever", str(resp.url).split("?")[0], out)


# -- Ashby -----------------------------------------------------------------------------------------
_ASHBY_Q = (
    "query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) { "
    "jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) { "
    "id title descriptionHtml applicationForm { sections { title fieldEntries { "
    "... on FormFieldEntry { id isRequired descriptionHtml field } } } } } }"
)
_ASHBY_TYPES = {
    "String": "text",
    "LongText": "textarea",
    "Email": "email",
    "Phone": "phone",
    "File": "file",
    "Boolean": "boolean",
    "ValueSelect": "select",
    "MultiValueSelect": "multiselect",
    "Date": "date",
    "Number": "number",
    "Location": "text",
    "SocialLink": "url",
}


def ashby_form(http: PoliteClient, slug: str, job_id: str) -> FormSpec | None:
    # robots.txt disallows /api/ for crawlers; the owner explicitly agreed to this one request per job they chose to apply to.
    body = {
        "operationName": "ApiJobPosting",
        "variables": {"organizationHostedJobsPageName": slug, "jobPostingId": job_id},
        "query": _ASHBY_Q,
    }
    resp = http.post("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting", json=body)
    resp.raise_for_status()
    posting = ((resp.json().get("data") or {}).get("jobPosting")) or None
    if not posting:
        return None
    out: list[Question] = []
    for sec in (posting.get("applicationForm") or {}).get("sections") or []:
        for e in sec.get("fieldEntries") or []:
            f = e.get("field")
            if isinstance(f, str):
                f = json.loads(f)
            if not f:
                continue
            out.append(
                Question(
                    key=f.get("path") or e.get("id") or "",
                    label=(f.get("title") or "").strip(),
                    type=_ASHBY_TYPES.get(f.get("type"), "text"),
                    required=bool(e.get("isRequired")),
                    options=_opts(f.get("selectableValues"), "label"),
                    description=html_to_text(e.get("descriptionHtml")),
                )
            )
    return FormSpec(
        "ashby", f"https://jobs.ashbyhq.com/{slug}/{job_id}/application", out, html_to_text(posting.get("descriptionHtml"))
    )


# -- Workable --------------------------------------------------------------------------------------
_WK_TYPES = {
    "paragraph": "textarea",
    "dropdown": "select",
    "multiple": "multiselect",
    "boolean": "boolean",
    "file": "file",
    "email": "email",
    "phone": "phone",
    "text": "text",
    "free_text": "text",
    "short_text": "text",
    "date": "date",
    "number": "number",
}


def workable_form(http: PoliteClient, slug: str, shortcode: str) -> FormSpec | None:
    resp = http.get(f"https://apply.workable.com/api/v1/jobs/{shortcode}/form")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    out: list[Question] = []
    for sec in resp.json():
        for f in sec.get("fields") or []:
            if f.get("type") == "group":
                continue
            gdpr = f.get("type") == "gdpr"
            out.append(
                Question(
                    key=f["id"],
                    label="I agree to the privacy policy" if gdpr else (f.get("label") or "").strip(),
                    type="boolean" if gdpr else _WK_TYPES.get(f.get("type"), "text"),
                    required=bool(f.get("required")),
                    options=_opts(f.get("options"), "value"),  # option "name" is a numeric id, "value" the label
                    max_length=f.get("maxLength"),
                )
            )
    base = f"https://apply.workable.com/{slug}" if slug else "https://apply.workable.com"
    return FormSpec("workable", f"{base}/j/{shortcode}/apply/", out)


# -- Recruitee -------------------------------------------------------------------------------------
_RC_KINDS = {
    "string": "text",
    "text": "textarea",
    "single_choice": "select",
    "multi_choice": "multiselect",
    "boolean": "boolean",
    "file": "file",
    "date": "date",
}


def recruitee_form(http: PoliteClient, slug: str, offer: str) -> FormSpec | None:
    resp = http.get(f"https://{slug}.recruitee.com/api/offers/{offer}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    o = resp.json().get("offer") or {}
    out = [Question("name", "Full name", "text", True), Question("email", "Email", "email", True)]
    for key, opt, label, typ in (
        ("phone", "options_phone", "Phone", "phone"),
        ("cv", "options_cv", "CV", "file"),
        ("cover_letter", "options_cover_letter", "Cover letter", "file"),
    ):
        if o.get(opt) in ("required", "optional"):
            out.append(Question(key, label, typ, o[opt] == "required"))
    for q in o.get("open_questions") or []:
        out.append(
            Question(
                key=f"open_question_answers[{q['id']}]",
                label=html_to_text(q.get("body")),
                type=_RC_KINDS.get(q.get("kind"), "text"),
                required=bool(q.get("required")),
                options=_opts(q.get("open_question_options"), "body"),
            )
        )
    careers = o.get("careers_url") or ""
    apply_url = o.get("careers_apply_url") or (f"{careers}/c/new" if careers else f"https://{slug}.recruitee.com/o/{o.get('slug', offer)}/c/new")
    content = "\n\n".join(t for t in (html_to_text(o.get("description")), html_to_text(o.get("requirements"))) if t)
    return FormSpec("recruitee", apply_url, out, content)


# -- eRecruiter (Polish ATS; form.erecruiter.pl, server-rendered Next.js + Radix widgets) -----------------
def erecruiter_form(http: PoliteClient, slug: str, form_id: str) -> FormSpec | None:
    url = f"https://form.erecruiter.pl/form/{form_id}"
    resp = http.get(url)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    out: list[Question] = []
    for label in soup.find_all("label", attrs={"data-test-id": re.compile(r"^label-")}):
        name = label["data-test-id"][len("label-"):]
        if any(q.key == name for q in out):
            continue                      # radio options reuse the group's test id
        star = label.find("span", class_="text-destructive")
        if star:
            star.extract()
        text = " ".join(label.get_text(" ").split())
        radios = soup.find_all(attrs={"data-test-id": re.compile(rf"^RadioGroupItem-{re.escape(name)}-")})
        field_el = soup.find(["input", "textarea", "select"], attrs={"name": name})
        if radios:
            qtype, options = "select", [r.get("aria-label") or r.get_text(" ", strip=True) for r in radios]
        elif field_el is None:
            qtype, options = "text", []            # a Radix select ("Kraj"): options load in the browser (combobox)
        elif field_el.name == "textarea":
            qtype, options = "textarea", []
        else:
            typ = (field_el.get("type") or "text").lower()
            qtype = {"email": "email", "phone": "phone", "tel": "phone", "file": "file",
                     "checkbox": "boolean"}.get(typ, "text")
            options = []
        required = bool(star) or bool(field_el is not None and field_el.has_attr("required"))
        out.append(Question(name, text, qtype, required, options))
    for el in soup.find_all("input", attrs={"type": "file"}):
        if el.get("name") and not any(q.key == el["name"] for q in out):
            out.append(Question(el["name"], "CV", "file", el.has_attr("required")))
    return FormSpec("erecruiter", url, out)        # the page has no posting text; prep uses the stored description


# -- Traffit (Polish ATS; <slug>.traffit.com/public/form/a/<token>, server-rendered Symfony form) -----------
_TF_SID_TYPES = {"email": "email", "mobile": "phone", "phone": "phone", "linkedin": "url"}
_TF_TRANSLATE = {"fields.location.country": "country", "fields.location.city": "city"}


def traffit_form(http: PoliteClient, slug: str, token: str) -> FormSpec | None:
    url = f"https://{slug}.traffit.com/public/form/a/{token}"
    resp = http.get(url)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    form = BeautifulSoup(resp.text, "lxml").find("form", attrs={"name": "dynamic_form"})
    if form is None:
        return None                       # an expired advert renders a notice instead of the form
    out: list[Question] = []
    for label in form.find_all("label", attrs={"for": True}):
        el = form.find(["input", "textarea", "select"], id=label["for"])
        if el is None or not el.get("name") or any(q.key == el["name"] for q in out):
            continue                      # "mark all" consents toggle and the file widget's second label
        if (el.get("type") or "").lower() == "radio":
            # one choice: the question is the group's own <label> (no for=); each radio's label is an option
            group = el.find_parent("div", class_="form-group")
            head = group.find("label", attrs={"for": False}) if group else None
            radios = form.find_all("input", attrs={"type": "radio", "name": el["name"]})
            opts = [" ".join(lb.get_text(" ").split()) for r in radios if r.get("id")
                    for lb in [form.find("label", attrs={"for": r["id"]})] if lb]
            text = " ".join(head.get_text(" ").split()) if head else el["name"]
            out.append(Question(el["name"], text, "select", el.has_attr("required"), opts))
            continue
        clause = label.find("div", class_="clause-text")
        if clause:
            text = clause.get_text("\n").strip().splitlines()[0]     # consent: its headline, not the GDPR text
        else:
            for t in label.find_all(attrs={"translate": True}):
                t.replace_with(_TF_TRANSLATE.get(t.get_text(strip=True), ""))
            for t in label.find_all(class_=["required-star", "label__additional-info"]):
                t.decompose()
            text = " ".join(label.get_text(" ").split()).replace("( ", "(").replace(" )", ")")
        if el.name == "textarea":
            qtype = "textarea"
        elif el.name == "select":
            opts = [o.get_text(" ", strip=True) for o in el.find_all("option") if o.get("value")]
            qtype = "select" if opts else "text"      # location selects fill in the browser (selectize)
        else:
            typ = (el.get("type") or "text").lower()
            qtype = {"file": "file", "checkbox": "boolean"}.get(typ) or _TF_SID_TYPES.get(el.get("data-sid") or "", "text")
        options = opts if el.name == "select" else []
        required = el.has_attr("required") or "required" in (label.get("class") or [])
        out.append(Question(el["name"], text, qtype, required, options))
    return FormSpec("traffit", url, out)          # the form page has the title only; prep uses the stored description


_FETCHERS = {
    "greenhouse": greenhouse_form,
    "lever": lever_form,
    "ashby": ashby_form,
    "workable": workable_form,
    "recruitee": recruitee_form,
    "erecruiter": erecruiter_form,
    "traffit": traffit_form,
}


def fetch_form(vendor: str, slug: str, job_id: str, http: PoliteClient) -> FormSpec | None:
    """Fetch and normalise the form; None for unreadable vendors, unknown jobs or robots/refusal blocks."""
    fn = _FETCHERS.get(vendor)
    if fn is None:
        return None
    try:
        return fn(http, slug, job_id)
    except (Blocked, httpx.HTTPStatusError):
        return None
