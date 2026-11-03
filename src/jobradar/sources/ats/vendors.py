"""Public company-ATS job board clients (formats verified live 2026-10-22).

Each ``fetch_<vendor>(http, slug, extra, wants_detail)`` returns (http_status, postings).
Details that are easy to get wrong: Workable (widget GET, not v3 GET), Teamtailor (`items`),
Workday (tenant subdomain + externalPath ids), Lever (EU host fallback), JazzHR (HTML).
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from jobradar.http.client import PoliteClient
from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import as_list, first, parse_date, to_float

Result = tuple[int, list[Posting]]
WantsDetail = Callable[[str], bool]


def _p(vendor: str, slug: str, job_id: str, **kw: Any) -> Posting:
    return Posting(source=f"ats_{vendor}", source_job_id=f"{slug}:{job_id}", ats_type=vendor, ats_slug=slug,
                   ats_job_id=str(job_id), **kw)


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


# ----------------------------------------------------------------- Greenhouse
def fetch_greenhouse(http: PoliteClient, slug: str, extra: dict, wants_detail: WantsDetail) -> Result:
    resp = http.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
    data = _json(resp) if resp.status_code == 200 else None
    if not isinstance(data, dict):
        return resp.status_code, []
    out = []
    company = extra.get("company_name") or slug
    for j in data.get("jobs") or []:
        offices = [o.get("location") or o.get("name") for o in j.get("offices") or [] if isinstance(o, dict)]
        loc = (j.get("location") or {}).get("name") or ""
        out.append(_p(
            "greenhouse", slug, str(j["id"]),
            title=j.get("title") or "", company=j.get("company_name") or company,
            url=j.get("absolute_url") or "", location_raw="; ".join(x for x in [loc, *offices] if x),
            description=html_to_text(j.get("content")), posted_at=parse_date(first(j.get("first_published"), j.get("updated_at"))),
            tags=[d.get("name") for d in j.get("departments") or [] if isinstance(d, dict) and d.get("name")],
        ))
    return 200, out


# ---------------------------------------------------------------------- Lever
def _lever_posts(slug: str, rows: list[dict], company: str) -> list[Posting]:
    out = []
    for j in rows:
        cats = j.get("categories") or {}
        sal = j.get("salaryRange") or {}
        desc = "\n\n".join(
            [j.get("descriptionPlain") or ""]
            + [f"{l.get('text', '')}\n{html_to_text(l.get('content'))}" for l in j.get("lists") or [] if isinstance(l, dict)]
            + [j.get("additionalPlain") or ""]
        )
        wp = (j.get("workplaceType") or "").lower()
        out.append(_p(
            "lever", slug, j["id"],
            title=j.get("text") or "", company=company, url=j.get("hostedUrl") or "", apply_url=j.get("applyUrl"),
            location_raw="; ".join(as_list(cats.get("allLocations")) or as_list(cats.get("location"))),
            countries=[j["country"]] if j.get("country") else [],
            workplace={"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}.get(wp),
            description=desc.strip(), posted_at=parse_date(j.get("createdAt")),
            employment_types=as_list(cats.get("commitment")),
            salary_min=to_float(sal.get("min")), salary_max=to_float(sal.get("max")),
            salary_currency=sal.get("currency"), salary_period=(sal.get("interval") or "").replace("per-", "") or None,
            tags=as_list(cats.get("team")),
        ))
    return out


def fetch_lever(http: PoliteClient, slug: str, extra: dict, wants_detail: WantsDetail) -> Result:
    hosts = ["api.eu.lever.co", "api.lever.co"] if extra.get("region") == "eu" else ["api.lever.co", "api.eu.lever.co"]
    status = 0
    for host in hosts:
        resp = http.get(f"https://{host}/v0/postings/{slug}", params={"mode": "json", "limit": 250})
        status = resp.status_code
        if status == 200 and isinstance(_json(resp), list):
            extra["region"] = "eu" if ".eu." in host else "global"
            return 200, _lever_posts(slug, resp.json(), extra.get("company_name") or slug)
        if status != 404:
            break
    return status, []


# ---------------------------------------------------------------------- Ashby
def fetch_ashby(http: PoliteClient, slug: str, extra: dict, wants_detail: WantsDetail) -> Result:
    resp = http.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"})
    data = _json(resp) if resp.status_code == 200 else None
    if not isinstance(data, dict):
        return resp.status_code, []
    out = []
    for j in data.get("jobs") or []:
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [s.get("location") for s in j.get("secondaryLocations") or [] if isinstance(s, dict)]
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        wp = (j.get("workplaceType") or "").lower()
        comp = j.get("compensation") or {}
        smin = smax = cur = per = None
        for c in comp.get("summaryComponents") or []:
            if isinstance(c, dict) and c.get("compensationType") in (None, "Salary"):
                smin, smax, cur = to_float(c.get("minValue")), to_float(c.get("maxValue")), c.get("currencyCode")
                per = (c.get("interval") or "").replace("1 ", "").lower() or None
                break
        workplace = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}.get(wp)
        if workplace is None and j.get("isRemote") is True:
            workplace = "remote"
        out.append(_p(
            "ashby", slug, j["id"],
            title=j.get("title") or "", company=extra.get("company_name") or slug,
            url=j.get("jobUrl") or "", apply_url=j.get("applyUrl"),
            location_raw="; ".join(x for x in locs if x) or addr.get("addressLocality") or "",
            countries=[addr["addressCountry"]] if len(str(addr.get("addressCountry") or "")) == 2 else [],
            workplace=workplace, description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            posted_at=parse_date(j.get("publishedAt")), employment_types=as_list(j.get("employmentType")),
            salary_min=smin, salary_max=smax, salary_currency=cur, salary_period=per,
            tags=[x for x in (j.get("department"), j.get("team")) if x],
        ))
    return 200, out


# ------------------------------------------------------------------- Workable
def fetch_workable(http: PoliteClient, slug: str, extra: dict, wants_detail: WantsDetail) -> Result:
    resp = http.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"})
    data = _json(resp) if resp.status_code == 200 else None
    if not isinstance(data, dict):
        return resp.status_code, []
    company = data.get("name") or extra.get("company_name") or slug
    out = []
    for j in data.get("jobs") or []:
        locs = [l for l in j.get("locations") or [] if isinstance(l, dict) and not l.get("hidden")]
        countries = [l.get("countryCode") for l in locs if l.get("countryCode")]
        loc_raw = "; ".join(", ".join(x for x in (l.get("city"), l.get("region"), l.get("country")) if x) for l in locs) \
            or ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        out.append(_p(
            "workable", slug, j.get("shortcode") or j.get("code") or j.get("url"),
            title=j.get("title") or "", company=company, url=j.get("url") or j.get("shortlink") or "",
            apply_url=j.get("application_url"), location_raw=loc_raw, countries=countries,
            workplace="remote" if j.get("telecommuting") else None,
            description=html_to_text(j.get("description")), posted_at=parse_date(first(j.get("published_on"), j.get("created_at"))),
            employment_types=as_list(j.get("employment_type")), tags=[x for x in (j.get("department"), j.get("function")) if x],
        ))
    return 200, out


# ------------------------------------------------------------------ Recruitee
def fetch_recruitee(http: PoliteClient, slug: str, extra: dict, wants_detail: WantsDetail) -> Result:
    resp = http.get(f"https://{slug}.recruitee.com/api/offers/")
    data = _json(resp) if resp.status_code == 200 else None
    if not isinstance(data, dict):
        return resp.status_code, []
    out = []
    for j in data.get("offers") or []:
        sal = j.get("salary") or {}
        workplace = "remote" if j.get("remote") else "hybrid" if j.get("hybrid") else "onsite" if j.get("on_site") else None
        locs = j.get("locations") or []
        countries = [l.get("country_code") for l in locs if isinstance(l, dict) and l.get("country_code")] or (
            [j["country_code"]] if j.get("country_code") else [])
        out.append(_p(
            "recruitee", slug, str(j.get("id")),
            title=j.get("title") or "", company=j.get("company_name") or extra.get("company_name") or slug,
            url=j.get("careers_url") or "", apply_url=j.get("careers_apply_url"),
            location_raw=j.get("location") or ", ".join(x for x in (j.get("city"), j.get("country")) if x),
            countries=[c.upper() for c in countries], workplace=workplace,
            description=html_to_text(f"{j.get('description') or ''}\n{j.get('requirements') or ''}"),
            posted_at=parse_date(first(j.get("published_at"), j.get("created_at"))),
            employment_types=as_list(j.get("employment_type_code")),
            salary_min=to_float(sal.get("min")), salary_max=to_float(sal.get("max")),
            salary_currency=sal.get("currency"), salary_period=sal.get("period"),
        ))
    return 200, out


# ------------------------------------------------------------------- Personio
def fetch_personio(http: PoliteClient, slug: str, extra: dict, wants_detail: WantsDetail) -> Result:
    status = 0
    for tld in (extra.get("tld") or "de", "com" if (extra.get("tld") or "de") == "de" else "de"):
        resp = http.get(f"https://{slug}.jobs.personio.{tld}/xml", params={"language": "en"})
        status = resp.status_code
        if status == 200 and resp.text.lstrip().startswith("<"):
            break
    else:
        return status, []
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        return status, []
    out = []
    for pos in root.iter("position"):
        g = lambda tag: (pos.findtext(tag) or "").strip()  # noqa: E731
        jid = g("id")
        offices = [g("office")] + [o.text.strip() for o in pos.findall("additionalOffices/office") if o.text]
        desc = "\n\n".join(
            f"{(d.findtext('name') or '').strip()}\n{html_to_text(d.findtext('value'))}"
            for d in pos.findall("jobDescriptions/jobDescription")
        )
        out.append(_p(
            "personio", slug, jid,
            title=g("name"), company=g("subcompany") or extra.get("company_name") or slug,
            url=f"https://{slug}.jobs.personio.{tld}/job/{jid}?language=en",
            location_raw="; ".join(x for x in offices if x), description=desc, posted_at=parse_date(g("createdAt")),
            employment_types=[x for x in (g("employmentType"), g("schedule")) if x],
            tags=[x for x in (g("department"), g("seniority")) if x],
        ))
    return 200, out


# ------------------------------------------------------------ SmartRecruiters
def fetch_smartrecruiters(http: PoliteClient, slug: str, extra: dict, wants_detail: WantsDetail) -> Result:
    out, offset, status = [], 0, 0
    while True:
        resp = http.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                        params={"limit": 100, "offset": offset})
        status = resp.status_code
        data = _json(resp) if status == 200 else None
        if not isinstance(data, dict):
            break
        content = data.get("content") or []
        for j in content:
            loc = j.get("location") or {}
            title = j.get("name") or ""
            desc = ""
            url = f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}"
            if wants_detail(title) and j.get("ref") and http.remaining > 5:
                dresp = http.get(j["ref"])
                d = _json(dresp) if dresp.status_code == 200 else None
                if isinstance(d, dict):
                    secs = (d.get("jobAd") or {}).get("sections") or {}
                    desc = "\n\n".join(html_to_text((secs.get(k) or {}).get("text")) for k in
                                       ("jobDescription", "qualifications", "additionalInformation"))
                    url = d.get("postingUrl") or url
            workplace = "remote" if loc.get("remote") else "hybrid" if loc.get("hybrid") else None
            out.append(_p(
                "smartrecruiters", slug, str(j.get("id")),
                title=title, company=(j.get("company") or {}).get("name") or extra.get("company_name") or slug,
                url=url, location_raw=loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("country")) if x),
                countries=[str(loc["country"]).upper()] if loc.get("country") else [], workplace=workplace,
                description=desc, posted_at=parse_date(j.get("releasedDate")),
                employment_types=as_list((j.get("typeOfEmployment") or {}).get("label")),
                tags=[x for x in ((j.get("department") or {}).get("label"), (j.get("experienceLevel") or {}).get("label")) if x],
            ))
        offset += len(content)
        if not content or offset >= int(data.get("totalFound") or 0) or offset >= 1000:
            break
    return (200 if out or status == 200 else status), out


# ------------------------------------------------------------------ Teamtailor
def fetch_teamtailor(http: PoliteClient, slug: str, extra: dict, wants_detail: WantsDetail) -> Result:
    resp = http.get(f"https://{slug}.teamtailor.com/jobs.json")
    data = _json(resp) if resp.status_code == 200 else None
    if not isinstance(data, dict):
        return resp.status_code, []
    out = []
    for item in data.get("items") or []:
        jp = item.get("_jobposting") or {}
        locs = []
        countries = []
        for jl in as_list(jp.get("jobLocation")):
            addr = (jl or {}).get("address") or {}
            locs.append(", ".join(x for x in (addr.get("addressLocality"), addr.get("addressCountry")) if x))
            if len(str(addr.get("addressCountry") or "")) == 2:
                countries.append(addr["addressCountry"])
        out.append(_p(
            "teamtailor", slug, str(item.get("id")),
            title=item.get("title") or "", company=extra.get("company_name") or ((jp.get("hiringOrganization") or {}).get("name")) or slug,
            url=item.get("url") or "", location_raw="; ".join(x for x in locs if x), countries=countries,
            workplace="remote" if jp.get("jobLocationType") == "TELECOMMUTE" else None,
            description=html_to_text(item.get("content_html")), posted_at=parse_date(item.get("date_published")),
            employment_types=as_list(jp.get("employmentType")),
        ))
    return 200, out


# -------------------------------------------------------------------- BambooHR
def fetch_bamboohr(http: PoliteClient, slug: str, extra: dict, wants_detail: WantsDetail) -> Result:
    resp = http.get(f"https://{slug}.bamboohr.com/careers/list")
    data = _json(resp) if resp.status_code == 200 else None
    if not isinstance(data, dict):
        return resp.status_code, []
    out = []
    for j in data.get("result") or []:
        loc = j.get("atsLocation") or j.get("location") or {}
        title = j.get("jobOpeningName") or ""
        desc = ""
        if wants_detail(title) and http.remaining > 5:
            d = _json(http.get(f"https://{slug}.bamboohr.com/careers/{j.get('id')}/detail"))
            if isinstance(d, dict):
                desc = html_to_text(((d.get("result") or {}).get("jobOpening") or {}).get("description"))
        out.append(_p(
            "bamboohr", slug, str(j.get("id")),
            title=title, company=extra.get("company_name") or slug, url=f"https://{slug}.bamboohr.com/careers/{j.get('id')}",
            location_raw=", ".join(str(x) for x in (loc.get("city"), loc.get("state"), loc.get("country")) if x),
            workplace="remote" if j.get("isRemote") else None, description=desc,
            employment_types=as_list(j.get("employmentStatusLabel")), tags=as_list(j.get("departmentLabel")),
        ))
    return 200, out


# ---------------------------------------------------------------------- Breezy
def fetch_breezy(http: PoliteClient, slug: str, extra: dict, wants_detail: WantsDetail) -> Result:
    resp = http.get(f"https://{slug}.breezy.hr/json")
    data = _json(resp) if resp.status_code == 200 else None
    if not isinstance(data, list):
        return resp.status_code, []
    out = []
    for j in data:
        loc = j.get("location") or {}
        country = loc.get("country") or {}
        out.append(_p(
            "breezy", slug, str(j.get("id") or j.get("friendly_id")),
            title=j.get("name") or "", company=(j.get("company") or {}).get("name") or extra.get("company_name") or slug,
            url=j.get("url") or "", location_raw=loc.get("name") or "",
            countries=[country.get("id")] if isinstance(country, dict) and len(str(country.get("id") or "")) == 2 else [],
            workplace="remote" if loc.get("is_remote") else None, posted_at=parse_date(j.get("published_date")),
            employment_types=as_list((j.get("type") or {}).get("name") if isinstance(j.get("type"), dict) else j.get("type")),
            tags=as_list(j.get("department")),
        ))
    return 200, out


# ---------------------------------------------------------------------- JazzHR
_JAZZ_LINK = re.compile(r'href="(?:https://[\w-]+\.applytojob\.com)?/apply/jobs/details/(\w+)[^"]*"[^>]*>\s*([^<]{3,200})<', re.I)


def fetch_jazzhr(http: PoliteClient, slug: str, extra: dict, wants_detail: WantsDetail) -> Result:
    resp = http.get(f"https://{slug}.applytojob.com/apply/jobs")
    if resp.status_code != 200:
        return resp.status_code, []
    out = []
    for jid, title in dict(_JAZZ_LINK.findall(resp.text)).items():
        out.append(_p("jazzhr", slug, jid, title=title.strip(), company=extra.get("company_name") or slug,
                      url=f"https://{slug}.applytojob.com/apply/jobs/details/{jid}"))
    return 200, out


# ---------------------------------------------------------------------- Workday
def fetch_workday(http: PoliteClient, slug: str, extra: dict, wants_detail: WantsDetail) -> Result:
    tenant = extra.get("tenant") or slug
    shard = extra.get("shard") or "wd1"
    site = extra["site"]
    base = f"https://{tenant}.{shard}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}"
    out: dict[str, Posting] = {}
    status = 0
    for text in extra.get("search", ["data", "analytics", "BI"]):
        offset = 0
        while offset < int(extra.get("max_results", 100)):
            resp = http.post(f"{api}/jobs", json={"appliedFacets": extra.get("facets", {}), "limit": 20,
                                                  "offset": offset, "searchText": text},
                             headers={"Accept": "application/json", "Content-Type": "application/json"})
            status = resp.status_code
            data = _json(resp) if status == 200 else None
            if not isinstance(data, dict):
                break
            posts = data.get("jobPostings") or []
            for jp in posts:
                path = jp.get("externalPath")
                if not path or path in out:
                    continue
                title = jp.get("title") or ""
                desc, country, loc = "", [], jp.get("locationsText") or ""
                if wants_detail(title) and http.remaining > 3:
                    d = _json(http.get(f"{api}{path}", headers={"Accept": "application/json"}))
                    info = (d or {}).get("jobPostingInfo") or {}
                    desc = html_to_text(info.get("jobDescription"))
                    loc = info.get("location") or loc
                    c = (info.get("country") or {}).get("descriptor")
                    country = [c] if c else []
                out[path] = _p(
                    "workday", tenant, path.rsplit("_", 1)[-1] if "_" in path else path,
                    title=title, company=extra.get("company_name") or tenant, url=f"{base}/{site}{path}",
                    location_raw=f"{loc}; {'; '.join(country)}".strip("; "), description=desc,
                    posted_at=parse_date(jp.get("postedOn")),
                )
            offset += len(posts)
            if not posts or offset >= int(data.get("total") or 0):
                break
    return (200 if out or status == 200 else status), list(out.values())


# ----------------------------------------------------------------- Softgarden
_SG_ROW = re.compile(r'class="matchElement"\s+id="job_id_(\d+)"(.*?)(?=class="matchElement"\s+id="job_id_|</form>|$)', re.S)
_SG_LINK = re.compile(r'<a href="[^"]*?/job/(\d+)/[^"]*"[^>]*>\s*(.*?)\s*</a>', re.S)
_SG_LOC = re.compile(r'class="location-view-item">\s*([^<]+?)\s*<')
_SG_DATE = re.compile(r'matchValue date">\s*(\d{2}\.\d{2}\.\d{2})')
_SG_CAT = re.compile(r'matchValue jobcategory">\s*([^<]*?)\s*<')
_LD_JSON = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def _ld_jobposting(html: str) -> dict:
    for m in _LD_JSON.finditer(html or ""):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        for x in as_list(d) if not isinstance(d, dict) else [d]:
            if isinstance(x, dict) and x.get("@type") == "JobPosting":
                return x
    return {}


def _sg_date(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%d.%m.%y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fetch_softgarden(http: PoliteClient, slug: str, extra: dict, wants_detail: WantsDetail) -> Result:
    """One request per board (server-rendered vacancy list, all rows in one page); detail = JSON-LD of /job/<id>."""
    resp = http.get(f"https://{slug}.softgarden.io/")
    if resp.status_code != 200:
        return resp.status_code, []
    if "matchElement" not in resp.text and "/vacancies" not in str(resp.url):
        return 404, []
    out = []
    company = extra.get("company_name") or slug
    for jid, body in _SG_ROW.findall(resp.text):
        lm = _SG_LINK.search(body)
        if not lm:
            continue
        title = html_to_text(lm.group(2)).strip()
        loc = "; ".join(dict.fromkeys(x.strip() for x in _SG_LOC.findall(body)))
        cat = (_SG_CAT.search(body) or [None, ""])[1]
        posted = _sg_date((_SG_DATE.search(body) or [None, ""])[1])
        desc, countries, employment = "", [], []
        if wants_detail(title) and http.remaining > 5:
            d = http.get(f"https://{slug}.softgarden.io/job/{jid}")
            ld = _ld_jobposting(d.text) if d.status_code == 200 else {}
            desc = html_to_text(ld.get("description"))
            employment = as_list(ld.get("employmentType"))
            posted = parse_date(ld.get("datePosted")) or posted
            org = (ld.get("hiringOrganization") or {}).get("name")
            company = org or company
            addrs = [((x or {}).get("address") or {}) for x in as_list(ld.get("jobLocation"))]
            if not loc:
                loc = "; ".join(dict.fromkeys(", ".join(str(y) for y in (a.get("addressLocality"), a.get("addressCountry")) if y)
                                              for a in addrs if a))
            countries = [a["addressCountry"] for a in addrs if len(str(a.get("addressCountry") or "")) == 2]
        out.append(_p("softgarden", slug, jid, title=title, company=company, url=f"https://{slug}.softgarden.io/job/{jid}",
                      location_raw=loc, countries=countries, description=desc, posted_at=posted,
                      employment_types=employment, tags=[cat] if cat else []))
    return 200, out


# --------------------------------------------------------------------- Traffit
def fetch_traffit(http: PoliteClient, slug: str, extra: dict, wants_detail: WantsDetail) -> Result:
    """Public career-page feed: 100 adverts per request (page size via X-Request-* headers), up to 3 pages."""
    out, page, pages = [], 1, 1
    status = 0
    while page <= min(pages, 3):
        resp = http.get(f"https://{slug}.traffit.com/public/job_posts/published",
                        headers={"X-Request-Page-Size": "100", "X-Request-Current-Page": str(page)})
        status = resp.status_code
        data = _json(resp) if status == 200 else None
        if not isinstance(data, list):
            break
        try:
            pages = int(resp.headers.get("x-result-total-pages") or 1)
        except ValueError:
            pages = 1
        for j in data:
            adv = j.get("advert") or {}
            opts = j.get("options") or {}
            vals = {v.get("field_id"): v.get("value") for v in adv.get("values") or [] if isinstance(v, dict)}
            locs = [l for l in adv.get("locations") or [] if isinstance(l, dict)]
            html = "\n".join(str(v) for k, v in vals.items() if k != "geolocation" and isinstance(v, str) and v.lstrip().startswith("<"))
            model = str(opts.get("_work_model") or "").lower()
            workplace = "remote" if (opts.get("remote") == "1" or model == "remote") else \
                "hybrid" if model == "hybrid" else "onsite" if model in ("onsite", "on-site", "stationary") else None
            visible = str(opts.get("_Salary_Visibility") or "").lower() == "yes"
            out.append(_p(
                "traffit", slug, str(adv.get("id") or j.get("id")),
                title=adv.get("name") or opts.get("_job_title") or "", company=extra.get("company_name") or slug,
                url=j.get("url") or "", apply_url=j.get("application_form"),
                location_raw="; ".join(dict.fromkeys(", ".join(x for x in (l.get("locality"), l.get("country")) if x) for l in locs)),
                countries=[str(l["iso"]).upper() for l in locs if len(str(l.get("iso") or "")) == 2],
                workplace=workplace, description=html_to_text(html),
                posted_at=parse_date(str(opts.get("opening_date") or "").replace("/", "-")) or parse_date(j.get("valid_start")),
                employment_types=as_list(opts.get("_Type_of_contract")),
                salary_min=to_float(opts.get("_Salary_MIN")) if visible else None,
                salary_max=to_float(opts.get("_Salary_MAX")) if visible else None,
                salary_currency=opts.get("_Salary_Currency") if visible else None,
                tags=as_list(opts.get("discipline")),
            ))
        page += 1
    return (200 if out or status == 200 else status), out


# ---------------------------------------------------------------------- Homerun
def fetch_homerun(http: PoliteClient, slug: str, extra: dict, wants_detail: WantsDetail) -> Result:
    """One Atom feed per company (feed.homerun.co/<slug>) with full descriptions."""
    resp = http.get(f"https://feed.homerun.co/{slug}")
    if resp.status_code != 200 or not resp.text.lstrip().startswith("<"):
        return resp.status_code if resp.status_code != 200 else 404, []
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        return 200, []
    ns = "{http://www.w3.org/2005/Atom}"
    company = (root.findtext(f"{ns}title") or "").strip() or extra.get("company_name") or slug
    out = []
    for e in root.iter(f"{ns}entry"):
        g = lambda tag: (e.findtext(f"{ns}{tag}") or "").strip()  # noqa: E731
        link = e.find(f"{ns}link")
        jid = g("id") or (link.get("href") if link is not None else "")
        out.append(_p(
            "homerun", slug, jid, title=g("title"), company=(e.findtext(f"{ns}author/{ns}name") or company).strip(),
            url=link.get("href") if link is not None else "", location_raw=(e.findtext(f"{ns}location/{ns}name") or "").strip(),
            description=html_to_text(g("description") or g("content")), posted_at=parse_date(g("updated").replace(" ", "T")),
            employment_types=[x for x in [(e.findtext(f"{ns}type/{ns}name") or "").strip()] if x],
            tags=[x for x in [(e.findtext(f"{ns}department/{ns}name") or "").strip()] if x],
        ))
    return 200, out


# -------------------------------------------------------------------------- JOIN
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _join_state(html: str) -> dict:
    m = _NEXT_DATA.search(html or "")
    try:
        pp = json.loads(m.group(1))["props"]["pageProps"] if m else {}
    except (ValueError, KeyError, TypeError):
        return {}
    return pp.get("initialState") or pp.get("pageData") or {}


def fetch_join(http: PoliteClient, slug: str, extra: dict, wants_detail: WantsDetail) -> Result:
    """Company page `__NEXT_DATA__` (5 jobs per page, `?page=N`, max 3 pages); detail = job page `descriptionHtml`."""
    out, page, pages, status = [], 1, 1, 0
    while page <= min(pages, 3):
        resp = http.get(f"https://join.com/companies/{slug}", params={"page": page} if page > 1 else None)
        status = resp.status_code
        if status != 200:
            break
        st = _join_state(resp.text)
        jobs = st.get("jobs") or {}
        pages = int((jobs.get("pagination") or {}).get("pageCount") or 1)
        company = (st.get("company") or {}).get("name") or extra.get("company_name") or slug
        for j in jobs.get("items") or []:
            title = j.get("title") or ""
            city = j.get("city") or {}
            wp = str(j.get("workplaceType") or "").lower()
            ctry = (j.get("country") or {}).get("iso3166")
            url = f"https://join.com/companies/{slug}/{j.get('idParam')}"
            desc = ""
            if wants_detail(title) and http.remaining > 5:
                d = http.get(url)
                job = _join_state(d.text).get("job") or {} if d.status_code == 200 else {}
                desc = html_to_text(job.get("descriptionHtml") or job.get("description"))
            out.append(_p(
                "join", slug, str(j.get("id")), title=title, company=company, url=url,
                location_raw=", ".join(x for x in (city.get("cityName"), city.get("countryName")) if x),
                countries=[ctry.upper()] if ctry and len(ctry) == 2 else [],
                workplace={"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}.get(wp) or ("remote" if j.get("remoteType") else None),
                description=desc, posted_at=parse_date(j.get("createdAt")),
                employment_types=as_list((j.get("employmentType") or {}).get("name")),
                tags=as_list((j.get("category") or {}).get("name")),
            ))
        page += 1
    return (200 if out or status == 200 else status), out


FETCHERS: dict[str, Callable[[PoliteClient, str, dict, WantsDetail], Result]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "recruitee": fetch_recruitee,
    "personio": fetch_personio,
    "smartrecruiters": fetch_smartrecruiters,
    "teamtailor": fetch_teamtailor,
    "bamboohr": fetch_bamboohr,
    "breezy": fetch_breezy,
    "jazzhr": fetch_jazzhr,
    "workday": fetch_workday,
    "softgarden": fetch_softgarden,
    "traffit": fetch_traffit,
    "homerun": fetch_homerun,
    "join": fetch_join,
}
ATS_HOSTS = {
    "greenhouse": "boards-api.greenhouse.io", "lever": "api.lever.co", "ashby": "api.ashbyhq.com",
    "workable": "apply.workable.com", "recruitee": "recruitee.com", "personio": "jobs.personio.de",
    "smartrecruiters": "api.smartrecruiters.com", "teamtailor": "teamtailor.com", "bamboohr": "bamboohr.com",
    "breezy": "breezy.hr", "jazzhr": "applytojob.com", "workday": "myworkdayjobs.com",
    "softgarden": "softgarden.io", "traffit": "traffit.com", "homerun": "homerun.co", "join": "join.com",
}
