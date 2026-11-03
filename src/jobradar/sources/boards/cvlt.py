"""cv.lt — Lithuanian job board (server-rendered HTML, no JSON-LD).

Sitemap: https://www.cv.lt/sitemap_cvl.xml (index) → ``sitemaps/sitemap_cvl_jobads_current.xml`` (~1.4k URLs
``/<title-slug>-<city>-<employer>-<id>``). ``lastmod`` is re-stamped, so the numeric id decides what is new, and the
slug runs through the matcher's cheap title gate before any detail fetch.
Detail: ``<title>`` = "<employer> <position>, <date>"; ``h1`` position; ``div.job-offer`` description; sidebar label/value
pairs "Miestas:" (city), "Nuotolinis darbas:" (Taip = remote / Ne = on site / Hibridinis = hybrid), optional
language rows; salary badge "4800 - 6700 €/mėn.". The apply button is ``/jobs/<id>/apply-2-ats?redirect=<base64 url>``:
decoding the base64 gives the employer's ATS link without any request.

Params: max_details (30).
"""

from __future__ import annotations

import base64
import html as html_lib
import re
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from jobradar.http.client import Blocked, BudgetExceeded
from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, parse_date, to_float
from jobradar.sources.generic.sitemap_jsonld import decode_body, parse_sitemap

SITE = "https://www.cv.lt"
INDEX = f"{SITE}/sitemap_cvl.xml"
_ID = re.compile(r"/(?P<slug>[^/?#]*?)-?(?P<id>\d{6,})/?$")
_APPLY = re.compile(r"apply-2-ats\?redirect=([A-Za-z0-9+/=_-]+)")
_MODE = {"taip": "remote", "ne": "onsite", "hibridinis": "hybrid"}
_LANG = {
    "vokiečių": "German", "prancūzų": "French", "olandų": "Dutch", "ispanų": "Spanish", "italų": "Italian",
    "portugalų": "Portuguese", "švedų": "Swedish", "danų": "Danish", "norvegų": "Norwegian", "suomių": "Finnish",
    "čekų": "Czech", "vengrų": "Hungarian", "rumunų": "Romanian", "rusų": "Russian", "latvių": "Latvian",
    "estų": "Estonian", "lietuvių": "Lithuanian",
}


def decode_apply(href_or_html: str) -> str | None:
    m = _APPLY.search(href_or_html)
    if not m:
        return None
    b = m.group(1)
    try:
        url = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode("utf-8")
    except ValueError:
        return None
    return url if url.startswith("http") else None


def _salary(text: str) -> tuple[float | None, float | None, str | None]:
    m = re.search(r"(\d[\d\s]*)(?:\s*-\s*(\d[\d\s]*))?\s*€\s*/\s*(mėn|val|m\.|met)", text)
    if not m:
        return None, None, None
    lo, hi = to_float(m.group(1)), to_float(m.group(2))
    period = {"mėn": "month", "val": "hour"}.get(m.group(3), "year")
    return lo, hi or lo, period


def parse_detail(html: str, source_id: str, job_id: str, url: str) -> Posting | None:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    if not h1:
        return None
    title = " ".join(h1.get_text(" ", strip=True).split())
    page_title = " ".join((soup.title.get_text() if soup.title else "").split())
    company, posted = "", None
    m = re.match(r"(.*?)\s+" + re.escape(title) + r",\s*(\d{4}-\d{2}-\d{2})\s*$", page_title)
    if m:
        company, posted = m.group(1).strip(), parse_date(m.group(2))
    body = soup.select_one("div.job-offer")
    description = html_to_text(str(body)) if body else ""
    labels: dict[str, str] = {}
    for div in soup.find_all("div"):
        t = div.get_text(" ", strip=True)
        if t.endswith(":") and len(t) < 40 and div.find_next_sibling("div") is not None and not div.find("div"):
            labels.setdefault(t[:-1].strip().lower(), div.find_next_sibling("div").get_text(" ", strip=True))
    mode = _MODE.get((labels.get("nuotolinis darbas") or "").strip().lower())
    city = labels.get("miestas", "")
    langs = [n for k, n in _LANG.items() if k in (labels.get("kalbos") or labels.get("užsienio kalbos") or "").lower()]
    if langs:
        description += "\n\nLanguages: " + " ".join(f"{n} required." for n in langs)
    sal_text = ""
    for el in soup.find_all(string=re.compile(r"€\s*/")):
        sal_text = el.strip()
        break
    lo, hi, period = _salary(sal_text)
    return Posting(
        source=source_id,
        source_job_id=job_id,
        title=title,
        company=company,
        url=url,
        apply_url=decode_apply(html),
        location_raw=f"{city}, Lithuania" if city else "Lithuania",
        countries=["LT"],
        cities=[city] if city else [],
        workplace=mode,
        description=description,
        posted_at=posted,
        salary_min=lo,
        salary_max=hi,
        salary_currency="EUR" if lo else None,
        salary_period=period,
        raw={"remote_label": labels.get("nuotolinis darbas")},
    )


@adapter("cvlt")
def cvlt(ctx: FetchContext) -> Iterator[Posting]:
    max_details = int(ctx.params.get("max_details", 30))
    try:
        resp = ctx.http.get(INDEX)
        if resp.status_code != 200:
            ctx.notes.append(f"sitemap index HTTP {resp.status_code}")
            return
        children, direct = parse_sitemap(decode_body(resp.content))
        entries = list(direct)
        for child in [c for c in children if "jobads_current" in c.loc][:1]:
            r2 = ctx.http.get(urljoin(INDEX, child.loc))
            if r2.status_code != 200:
                ctx.notes.append(f"sitemap HTTP {r2.status_code}")
                continue
            entries += parse_sitemap(decode_body(r2.content))[1]
    except (Blocked, BudgetExceeded) as exc:
        ctx.notes.append(f"sitemap stopped: {exc}")
        return
    pending: list[tuple[int, str]] = []
    seen: set[str] = set()
    for e in entries:
        m = _ID.search(e.loc)
        if not m or m.group("id") in seen:
            continue
        jid = m.group("id")
        seen.add(jid)
        if not ctx.wants_detail(html_lib.unescape(m.group("slug")).replace("-", " ")):
            ctx.skipped += 1
            continue
        if ctx.is_known(ctx.spec.id, jid):
            ctx.mark_seen(ctx.spec.id, jid)
            continue
        pending.append((int(jid), e.loc))
    pending.sort(reverse=True)
    if len(pending) > max_details:
        ctx.notes.append(f"{len(pending)} new title-relevant, fetching the newest {max_details}")
    for jid, loc in pending[:max_details]:
        try:
            r = ctx.http.get(loc)
        except (Blocked, BudgetExceeded) as exc:
            ctx.notes.append(f"detail stopped: {exc}")
            return
        if r.status_code != 200:
            ctx.notes.append(f"detail HTTP {r.status_code}")
            continue
        post = parse_detail(r.text, ctx.spec.id, str(jid), loc)
        if post:
            yield post
        else:
            ctx.notes.append("detail not parsed")
