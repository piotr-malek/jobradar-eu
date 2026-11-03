"""Purpose-track sources: UN Careers (careers.un.org) and EU Careers (eu-careers.europa.eu).

Both only add structured grade/category tags (``grade:P-3``, ``category:PD``, ``grade:FG IV``);
the matcher applies the grade filter. Obvious non-jobs (UN internships) are counted in ``ctx.skipped``.
"""

from __future__ import annotations

import html as _html
import re
from typing import Any, Iterator

from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, parse_date
from jobradar.sources.boards.public_boards import _country_from_place

# ===================================================================== UN Careers
UN_LIST = "https://careers.un.org/api/public/opening/jo/list/filteredV2/en"
UN_JOB = "https://careers.un.org/jobSearchDescription/{id}?language=en"
# The shared city index reads these as the wrong place (or not at all).
_UN_STATION_COUNTRY = {"VALENCIA": "ES", "HQ AMMAN": "JO", "SANTO DOMINGO": "DO", "FO WEST BANK": "PS",
                       "GEBZE": "TR", "PORT-AU-PRINCE - LOCAL": "HT", "KINGSTON": "JM", "SUVA": "FJ"}
_HOME = re.compile(r"Work Location</div><div class='jobPostingItemContent'>[^<]*home[- ]?based", re.I)
_UN_SKIP_CATEGORIES = {"INT"}


def _station_countries(stations: list[str]) -> list[str]:
    from jobradar.normalize.location import extract_places

    out: list[str] = []
    for st in stations[:3]:
        iso = _UN_STATION_COUNTRY.get(st.upper())
        if not iso:
            found = extract_places(st.title())[0]
            iso = next(iter(found)) if len(found) == 1 else None
        if iso and iso not in out:
            out.append(iso)
    return out


@adapter("un_careers")
def un_careers(ctx: FetchContext) -> Iterator[Posting]:
    """UN Secretariat + funds/programmes on careers.un.org. The list payload already carries the full
    description, so a run is ``ceil(openings / 100)`` POSTs and no detail fetches."""
    p = ctx.params
    per_page = int(p.get("per_page", 100))
    skip_cats = set(p.get("skip_categories") or _UN_SKIP_CATEGORIES)
    seen: set[str] = set()
    for page in range(int(p.get("max_pages", 8))):
        body = {"filterConfig": {}, "pagination": {"page": page, "itemPerPage": per_page,
                                                    "sortBy": "startDate", "sortDirection": -1}}
        data = (ctx.http.post_json(UN_LIST, json=body) or {}).get("data") or {}
        rows = data.get("list") or []
        for j in rows:
            jid = str(j.get("jobId") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            cat = str(j.get("categoryCode") or "")
            if cat in skip_cats:
                ctx.skipped += 1
                continue
            raw_desc = j.get("jobDescription") or ""
            stations = [(s.get("description") or "").strip() for s in j.get("dutyStation") or []
                        if isinstance(s, dict)]
            stations = [s for s in stations if s]
            home = bool(_HOME.search(raw_desc))
            level = str(j.get("jobLevel") or "").strip()
            tags = []
            if level:
                tags.append(f"grade:{level}")
            if cat:
                tags.append(f"category:{cat}")
            fam = (j.get("jf") or {}).get("Name") or j.get("jobFamilyCode")
            if fam:
                tags.append(str(fam))
            end = j.get("endDate") or ""
            desc = html_to_text(raw_desc)
            if end:
                desc = f"Deadline: {str(end)[:10]}\n{desc}"
            loc = " | ".join(s.title() for s in stations[:5])
            if len(stations) > 5:
                loc += f" | +{len(stations) - 5} more"
            yield Posting(
                source=ctx.spec.id,
                source_job_id=jid,
                title=(j.get("postingTitle") or j.get("jobTitle") or "").strip(),
                company=((j.get("dept") or {}).get("name") or "United Nations").strip(),
                url=UN_JOB.format(id=jid),
                location_raw=("Home-based" + (f" ({loc})" if loc else "")) if home else loc,
                countries=_station_countries(stations),
                workplace="remote" if home else None,
                description=desc,
                posted_at=parse_date(j.get("startDate")),
                employment_types=[t for t in [(j.get("recrttype") or {}).get("name")] if t],
                tags=tags,
                raw={"category": cat, "level": level, "family": j.get("jobFamilyCode"),
                     "deadline": end, "hiring_entity": (j.get("dept") or {}).get("name")},
            )
        if len(rows) < per_page or (page + 1) * per_page >= int(data.get("count") or 0):
            break


# ==================================================================== EU Careers
EU_BASE = "https://eu-careers.europa.eu"
# /en/job-opportunities/open-vacancies 302s to this; hit the target directly (saves a request per page).
EU_LIST = EU_BASE + "/jobs_en?page={page}"
_EU_ITEM = re.compile(r'<div class="eujmp-vacancy-result">(.*?)</ul></div></div>', re.S)
_EU_TOP = re.compile(r'<ul class="eujmp-vacancy-result__top-meta">(.*?)</ul>', re.S)
_EU_BOTTOM = re.compile(r'<ul class="eujmp-vacancy-result__bottom-meta">(.*)', re.S)
_EU_LINK = re.compile(r'<a href="(/jobs/[^"]+)"[^>]*eujmp-vacancy-result__title">(.*?)</a>', re.S)
_LI = re.compile(r"<li>(.*?)</li>", re.S)
_TAG = re.compile(r"<[^>]+>")
_EU_FIELD = re.compile(r"<p[^>]*><strong>([^<:]+):</strong>\s*(.*?)</p>", re.S)
# Titles worth a detail fetch even when the purpose title patterns don't know them (the listing has no domain).
_EU_IT_TITLE = re.compile(
    r"\b(it|ict|data|statistic\w*|information|digital|cyber\w*|software|systems?|database|developer|"
    r"analyst|gis|geospatial|web|cloud|network|architect|engineer)\b", re.I)


def _txt(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG.sub(" ", s))).strip()


def parse_eu_list(page: str) -> list[dict[str, Any]]:
    rows = []
    for m in _EU_ITEM.finditer(page):
        blk = m.group(1) + "</ul>"
        link, top, bot = _EU_LINK.search(blk), _EU_TOP.search(blk), _EU_BOTTOM.search(blk)
        if not (link and top and bot):
            continue
        tl = [_txt(x) for x in _LI.findall(top.group(1))]
        bl = [_txt(x) for x in _LI.findall(bot.group(1))]
        dt = re.search(r'datetime="([^"]+)"', top.group(1))
        rows.append({
            "path": link.group(1), "title": _txt(link.group(2)),
            "institution": tl[0] if tl else "", "deadline": dt.group(1) if dt else None,
            "contract": bl[0] if bl else "", "grade": bl[1] if len(bl) > 2 else "",
            "location": bl[-1] if bl else "",
        })
    return rows


def parse_eu_detail(page: str) -> dict[str, Any]:
    a = page.find("eujmp-vacancy--full")
    b = page.find("ecl-site-footer", a)
    block = page[a:b] if a >= 0 else page
    fields = {k.strip(): _txt(v) for k, v in _EU_FIELD.findall(block)}
    # Free-text body (some agencies inline it; most attach a PDF): everything after the "Back" link.
    body = ""
    if "ecl-u-mb-2xl" in block:
        rest = re.sub(r"<script.*?</script>", "", block.split("ecl-u-mb-2xl", 1)[1], flags=re.S)
        rest = rest.split("</a>", 1)[-1]  # drop the Back link
        if "ecl-file" not in rest[:200]:
            body = html_to_text(rest)
    link = re.search(r'href="(https?://[^"]+)"[^>]*>(?:\s|<[^>]+>)*Vacancy link', block)
    return {"fields": fields, "body": body, "vacancy_link": link.group(1) if link else None}


@adapter("eu_careers")
def eu_careers(ctx: FetchContext) -> Iterator[Posting]:
    """EU institutions + agencies. Listing pages are server-rendered (20/page, ~7 pages); the detail page
    adds Domain/Reference/DG and, for agencies, a text body. Details are fetched only for wanted or IT-ish
    titles that are not stored yet (the listing carries no domain, so the title is the only pre-filter)."""
    p = ctx.params
    detail_cap = int(p.get("max_details", 38))
    listed: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for page in range(int(p.get("max_pages", 9))):
        rows = parse_eu_list(ctx.http.get(EU_LIST.format(page=page)).text)
        fresh = [r for r in rows if r["path"] not in seen_paths]
        if not fresh:
            break
        seen_paths.update(r["path"] for r in fresh)
        listed.extend(fresh)
        if len(rows) < 20:
            break
    details = 0
    for r in listed:
        slug = r["path"].rsplit("/", 1)[-1].removesuffix("_en")
        if ctx.is_known(ctx.spec.id, slug):
            ctx.mark_seen(ctx.spec.id, slug)
            continue
        title = r["title"]
        wanted = ctx.wants_detail(title) or bool(_EU_IT_TITLE.search(title))
        if not wanted or details >= detail_cap or ctx.http.remaining < 1:
            ctx.skipped += 1
            continue
        det = parse_eu_detail(ctx.http.get(EU_BASE + r["path"]).text)
        details += 1
        f = det["fields"]
        domain = f.get("Domain", "")
        grade = f.get("Grade") or r["grade"]
        contract = f.get("Contract type") or r["contract"]
        loc = f.get("Location") or r["location"]
        lines = [f"Institution: {f.get('Institution') or r['institution']}", f"Location: {loc}",
                 f"Domain: {domain}", f"Contract type: {contract}", f"Grade: {grade}"]
        for k in ("Reference", "Application deadline", "Directorate-General or Department", "Unit", "Salary"):
            if f.get(k):
                lines.append(f"{k}: {f[k]}")
        if det["vacancy_link"]:
            lines.append(f"Vacancy link: {det['vacancy_link']}")
        desc = "\n".join(lines) + ("\n\n" + det["body"] if det["body"] else "")
        tags = [f"grade:{grade}"] if grade else []
        if domain:
            tags.append(domain)
        iso = _country_from_place(loc)
        yield Posting(
            source=ctx.spec.id,
            source_job_id=slug,
            title=title,
            company=f.get("Institution") or r["institution"] or "European Union",
            url=EU_BASE + r["path"],
            apply_url=det["vacancy_link"],
            location_raw=loc,
            countries=[iso] if iso else [],
            cities=[loc.split(",")[0].strip()] if loc and iso else [],
            description=desc,
            employment_types=[contract] if contract else [],
            tags=tags,
            raw={"deadline": r["deadline"], "domain": domain, "reference": f.get("Reference")},
        )
