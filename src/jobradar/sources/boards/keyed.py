"""Sources that need the user's API keys: TheirStack, Apify (LinkedIn), ITJobs.pt.

Budgets matter here (free tiers):
- TheirStack free: 200 credits/month, 1 credit per job returned → weekly, ≤ 40 jobs/run.
- Apify free: $5/month; LinkedIn actor charges $0.002/result on the free plan → ≤ 120 results/run every 2 days.
- ITJobs.pt: free key, polite use.
"""

from __future__ import annotations

from typing import Iterator
from urllib.parse import urlencode

from jobradar.models import Posting
from jobradar.normalize.location import ELIGIBLE
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, default_terms, as_list, first, parse_date, to_float


# ---------------------------------------------------------------- TheirStack
THEIRSTACK_FREE_PAGE_MAX = 25  # free plan: more than 25 results per page → 403 E-020 "Premium functionality limitation"


@adapter("theirstack")
def theirstack(ctx: FetchContext) -> Iterator[Posting]:
    """TheirStack job API (key; one credit per job returned): recent jobs in eligible countries whose titles match the active packs.

    Pages of a fixed size (the API's page N = offset N × limit); a size that divides `limit` wastes no credits."""
    total = int(ctx.params.get("limit", 40))
    per_page = min(int(ctx.params.get("per_page", 20)), THEIRSTACK_FREE_PAGE_MAX, total)
    yielded = page = 0
    while yielded < total:
        rows = _theirstack_page(ctx, page, per_page)
        for j in rows[: total - yielded]:
            yield _theirstack_posting(ctx, j)
        yielded += len(rows)
        if len(rows) < per_page:
            break
        page += 1


def _theirstack_page(ctx: FetchContext, page: int, per_page: int) -> list[dict]:
    key = ctx.secrets["THEIR_STACK_API_KEY"]
    body = {
        "page": page,
        "limit": per_page,
        "posted_at_max_age_days": int(ctx.params.get("max_age_days", 7)),
        "job_country_code_or": ctx.params.get("countries", sorted(ELIGIBLE)),
        "job_title_or": ctx.params.get("titles") or default_terms(6),
        "include_total_results": False,
        "blur_company_data": False,
    }
    if ctx.params.get("technologies"):  # optional stack filter (TheirStack technology slugs), AND-ed with the titles
        body["job_technology_slug_or"] = ctx.params["technologies"]
    data = ctx.http.post_json("https://api.theirstack.com/v1/jobs/search",
                              headers={"Authorization": f"Bearer {key}"}, json=body)
    return data.get("data") or []


def _theirstack_posting(ctx: FetchContext, j: dict) -> Posting:
    comp = j.get("company_object") or {}
    remote, hybrid = j.get("remote"), j.get("hybrid")
    return Posting(
        source=ctx.spec.id,
        source_job_id=str(j.get("id")),
        title=j.get("job_title") or "",
        company=first(comp.get("name"), j.get("company")) or "",
        url=first(j.get("final_url"), j.get("source_url"), j.get("url")) or "",
        location_raw=first(j.get("location"), j.get("short_location")) or "",
        countries=[j["country_code"]] if j.get("country_code") else [],
        workplace="remote" if remote else "hybrid" if hybrid else None,
        description=html_to_text(j.get("description")),
        posted_at=parse_date(j.get("date_posted")),
        employment_types=as_list(j.get("employment_statuses")),
        salary_min=to_float(j.get("min_annual_salary")),
        salary_max=to_float(j.get("max_annual_salary")),
        salary_currency=j.get("salary_currency"),
        salary_period="year" if j.get("min_annual_salary") else None,
        tags=as_list(j.get("technology_slugs"))[:20],
    )


# ------------------------------------------------------ Apify LinkedIn actor
def linkedin_search_url(keywords: str, location: str, work_type: str | None = None, hours: int = 48) -> str:
    q = {"keywords": keywords, "location": location, "f_TPR": f"r{hours * 3600}"}
    if work_type:
        q["f_WT"] = {"onsite": "1", "remote": "2", "hybrid": "3"}[work_type]
    return "https://www.linkedin.com/jobs/search/?" + urlencode(q)


@adapter("apify_linkedin")
def apify_linkedin(ctx: FetchContext) -> Iterator[Posting]:
    key = ctx.secrets["APIFY_API_KEY"]
    searches = ctx.params.get("searches") or [
        {"keywords": k, "location": ctx.params.get("location", "European Union"), "work_type": ctx.params.get("work_type")}
        for k in (ctx.params.get("keywords") or default_terms(5))]
    urls = [linkedin_search_url(s["keywords"], s["location"], s.get("work_type"), int(s.get("hours", 48)))
            for s in searches]
    per_url = int(ctx.params.get("limit_per_search", 20))
    actor = ctx.params.get("actor", "curious_coder~linkedin-jobs-scraper")
    resp = ctx.http.post(
        f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items",
        params={"token": key, "timeout": 280, "maxItems": per_url * len(urls)},
        json={"urls": urls, "limitPerSource": per_url, "scrapeCompany": False},
        timeout=300,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"apify HTTP {resp.status_code}: {resp.text[:200]}")
    for j in resp.json() or []:
        jid = str(first(j.get("id"), j.get("jobId"), j.get("link")))
        workplace = (first(j.get("workplaceType"), j.get("workType")) or "").lower()
        yield Posting(
            source=ctx.spec.id,
            source_job_id=jid,
            title=first(j.get("title"), j.get("jobTitle")) or "",
            company=first(j.get("companyName"), (j.get("company") or {}).get("name") if isinstance(j.get("company"), dict) else j.get("company")) or "",
            url=first(j.get("link"), j.get("jobUrl"), j.get("url")) or "",
            apply_url=j.get("applyUrl") or None,
            location_raw=first(j.get("location"), j.get("formattedLocation")) or "",
            workplace="remote" if "remote" in workplace else "hybrid" if "hybrid" in workplace else
            "onsite" if "site" in workplace else None,
            description=first(j.get("descriptionText"), html_to_text(j.get("descriptionHtml"))) or "",
            posted_at=parse_date(first(j.get("postedAt"), j.get("publishedAt"), j.get("listedAt"))),
            employment_types=as_list(j.get("employmentType")),
            tags=[x for x in (j.get("seniorityLevel"), j.get("industries")) if isinstance(x, str)],
            raw={"salaryInfo": j.get("salaryInfo"), "applicantsCount": j.get("applicantsCount")},
        )


# ------------------------------------------------------------------- ITJobs.pt
@adapter("itjobs_pt")
def itjobs_pt(ctx: FetchContext) -> Iterator[Posting]:
    key = ctx.secrets["ITJOBS_PT_API_KEY"]
    seen: set[str] = set()
    for q in ctx.params.get("queries") or default_terms(5):
        data = ctx.http.get_json("https://api.itjobs.pt/job/search.json",
                                 params={"api_key": key, "q": q, "limit": int(ctx.params.get("limit", 100))})
        for j in data.get("results") or []:
            jid = str(j.get("id"))
            if jid in seen:
                continue
            seen.add(jid)
            locs = [l.get("name") for l in j.get("locations") or [] if isinstance(l, dict) and l.get("name")]
            wm = str(j.get("workModel") or "").lower()
            yield Posting(
                source=ctx.spec.id,
                source_job_id=jid,
                title=j.get("title") or "",
                company=((j.get("company") or {}).get("name")) or "",
                url=f"https://www.itjobs.pt/oferta/{jid}/{j.get('slug') or ''}".rstrip("/"),
                location_raw=", ".join(locs) or "Portugal",
                countries=["PT"],
                workplace="remote" if "remote" in wm else "hybrid" if "hybrid" in wm else
                "onsite" if wm in ("presential", "onsite", "on-site") else None,
                description=html_to_text(j.get("body")),
                posted_at=parse_date(first(j.get("publishedAt"), j.get("updatedAt"))),
                employment_types=[t.get("name") for t in j.get("types") or [] if isinstance(t, dict)],
                salary_min=to_float(j.get("salaryMin")),
                salary_max=to_float(j.get("salaryMax")),
                salary_currency="EUR" if j.get("salaryMin") else None,
                salary_period="month" if j.get("salaryMin") else None,
                raw={"workModel": j.get("workModel"), "contracts": j.get("contracts")},
            )
