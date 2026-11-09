"""Generic RSS / Atom job-feed adapter (WordPress, WP Job Manager, Hugo, custom feeds).

Feeds are parsed with a *recovering* XML parser: job-board feeds routinely contain undefined HTML
entities (``&nbsp;``) or stray control characters that make strict parsers give up. Besides RSS
<item> and Atom <entry>, the aggregator-style ``<jobs><job>`` XML feed (Indeed/Jooble format, e.g.
SwissDevJobs / GermanTechJobs ``/job_feed.xml``) is read the same way.

Params:
  feeds: [url | {url, workplace?, remote_scope?, category?}]   or feed_template + the packs' boards.<id>.categories
  title_split: {seps: [" // ", " at "], company: right|left}  split "Title // Company" style titles;
                the *last* occurrence is used when company is on the right (``rsplit``)
  company_field / location_field: [localname, ...]            item child elements (any namespace),
                e.g. ``company`` / ``location`` (WP Job Manager themes), ``author`` / ``creator``
  country_field: [localname, ...]                              country name/code element → countries
  workplace: remote                                            only for remote-only boards (explicit)
  remote_scope: str                                            e.g. "Europe" for EU-remote boards
  include_categories / exclude_categories: [regex]             filter on <category> values
  max_items: int (200)                                         per feed
  detail: bool (false)                                         fetch the item page for title-relevant
                items (``ctx.wants_detail``) not stored yet and merge its schema.org JobPosting JSON-LD;
                known title-relevant items are then skipped (no feed-level re-yield → no version churn)
  max_details: int (15)                                        detail pages per run
  id_from: guid|link (guid)                                    stable source_job_id
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any, Iterable, Iterator

from lxml import etree

from jobradar.http.client import Blocked, BudgetExceeded
from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, as_list, first, parse_date
from jobradar.sources.generic.sitemap_jsonld import country_iso2, extract_jobposting, jobposting_to_posting

ATOM = "{http://www.w3.org/2005/Atom}"
_PARSER = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=True)


# ------------------------------------------------------------------ feed parsing
def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _text(node: etree._Element | None) -> str:
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


def parse_feed(content: bytes) -> list[dict[str, Any]]:
    """RSS 2.0 <item> / Atom <entry> → list of {localname: text | [texts]} plus link/attrs.

    Repeated elements (category) become lists; Atom <link href> is resolved; every child is
    keyed by its local name so namespaced WP Job Manager fields (``job_listing:company``) are
    reachable as ``company``.
    """
    if not content or not content.strip():
        return []
    try:
        root = etree.fromstring(content.lstrip(), _PARSER)
    except etree.XMLSyntaxError:
        return []
    if root is None:
        return []
    nodes = (root.findall(".//item") or root.findall(f".//{ATOM}entry") or root.findall(".//entry")
             or root.findall(".//job"))
    out: list[dict[str, Any]] = []
    for node in nodes:
        rec: dict[str, Any] = {"categories": []}
        for child in node:
            name = _local(child.tag)
            if not name:
                continue
            if name == "link" and child.get("href"):
                if child.get("rel") in (None, "alternate"):
                    rec.setdefault("link", child.get("href"))
                continue
            if name == "category":
                val = child.get("term") or _text(child)
                if val:
                    rec["categories"].append(html_lib.unescape(val))
                continue
            if name in ("content", "encoded") and name in rec:
                continue
            val = _text(child)
            if name == "author" and not val:
                continue
            if name == "author" and len(child):
                val = _text(child.find(f"{ATOM}name")) or val
            rec.setdefault(name, val)
            if child.get("url") and name in ("enclosure", "content", "thumbnail"):
                rec.setdefault(f"{name}_url", child.get("url"))
        out.append(rec)
    return out


def split_title(raw: str, rule: dict[str, Any] | None) -> tuple[str, str]:
    """('Data Engineer', 'Acme') from 'Data Engineer // Acme' given {seps: [' // '], company: 'right'}."""
    title = html_lib.unescape(raw or "").strip()
    if not rule:
        return title, ""
    side = rule.get("company", "right")
    for sep in as_list(rule.get("seps") or rule.get("sep")):
        if sep and sep in title:
            if side == "left":
                company, _, rest = title.partition(sep)
                return rest.strip(), company.strip()
            rest, _, company = title.rpartition(sep)
            return rest.strip(), company.strip()
    return title, ""


def _field(rec: dict[str, Any], names: Iterable[str]) -> str:
    for n in names:
        v = rec.get(str(n).lower())
        if isinstance(v, str) and v.strip():
            return html_to_text(v).strip()
    return ""


def _compile(patterns: Any) -> list[re.Pattern[str]]:
    return [re.compile(p, re.I) for p in as_list(patterns) if p]


# ------------------------------------------------------------------ JSON-LD detail merge
def merge_jobposting(post: Posting, page: str, *, keep_title: bool = True) -> bool:
    """Fill empty/short fields of ``post`` from the page's schema.org JobPosting. True if found."""
    jp = extract_jobposting(page)
    if not jp:
        return False
    d = jobposting_to_posting(jp, source=post.source, source_job_id=post.source_job_id, url=post.url)
    if not keep_title and d.title:
        post.title = d.title
    post.company = post.company or d.company
    if len(d.description) > len(post.description):
        post.description = d.description
    post.location_raw = post.location_raw or d.location_raw
    post.countries = post.countries or d.countries
    post.cities = post.cities or d.cities
    post.workplace = post.workplace or d.workplace
    post.remote_scope = post.remote_scope or d.remote_scope
    post.posted_at = post.posted_at or d.posted_at
    post.employment_types = post.employment_types or d.employment_types
    if post.salary_min is None and post.salary_max is None:
        post.salary_min, post.salary_max = d.salary_min, d.salary_max
        post.salary_currency, post.salary_period = d.salary_currency, d.salary_period
    post.tags = list(dict.fromkeys(post.tags + d.tags))
    post.raw = {**(post.raw or {}), "jsonld": d.raw}
    return True


# ------------------------------------------------------------------ adapter
@adapter("rss_feed")
def rss_feed(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    split_rule = p.get("title_split")
    company_fields = as_list(p.get("company_field"))
    location_fields = as_list(p.get("location_field"))
    country_fields = as_list(p.get("country_field"))
    include = _compile(p.get("include_categories"))
    exclude = _compile(p.get("exclude_categories"))
    max_items = int(p.get("max_items", 200))
    want_detail = bool(p.get("detail", False))
    max_details = int(p.get("max_details", 15))
    id_from = p.get("id_from", "guid")
    seen: set[str] = set()
    details = 0
    detail_blocked = False
    refused: Blocked | None = None  # raised at the end if every feed was refused, so the run reads "blocked"
    any_feed = False

    feeds = as_list(p.get("feeds"))
    if not feeds and p.get("feed_template"):            # one feed per category the user's role packs name
        from jobradar import config

        cats = config.board_map(ctx.spec.id).get("categories") or as_list(p.get("default_categories"))
        feeds = [p["feed_template"].format(category=c) for c in cats]
    for feed in feeds:
        spec = feed if isinstance(feed, dict) else {"url": feed}
        url = spec["url"]
        try:
            resp = ctx.http.get(url)
        except Blocked as exc:
            ctx.notes.append(f"{url}: blocked ({exc.reason} {exc.status})")
            refused = exc
            continue
        if resp.status_code != 200:
            ctx.notes.append(f"{url}: HTTP {resp.status_code}")
            continue
        any_feed = True
        items = parse_feed(resp.content)
        if not items:
            ctx.notes.append(f"{url}: no items parsed")
            continue
        workplace = spec.get("workplace", p.get("workplace"))
        remote_scope = spec.get("remote_scope", p.get("remote_scope"))
        for rec in items[:max_items]:
            cats = rec.get("categories") or []
            if include and not any(rx.search(c) for rx in include for c in cats):
                continue
            if exclude and any(rx.search(c) for rx in exclude for c in cats):
                continue
            link = (rec.get("link") or rec.get("guid") or rec.get("id") or "").strip()
            jid = (rec.get("guid") or rec.get("id") or link) if id_from == "guid" else (link or rec.get("guid"))
            if not link or not jid or jid in seen:
                continue
            seen.add(jid)
            title, company = split_title(rec.get("title", ""), split_rule)
            company = _field(rec, company_fields) or company
            location = _field(rec, location_fields)
            country = country_iso2(_field(rec, country_fields)) if country_fields else None
            body = first(rec.get("encoded"), rec.get("content"), rec.get("description"), rec.get("summary")) or ""
            post = Posting(
                source=ctx.spec.id,
                source_job_id=str(jid),
                title=title,
                company=company,
                url=link,
                location_raw=location,
                countries=[country] if country else [],
                workplace=workplace,
                remote_scope=remote_scope or (location if workplace == "remote" and location else None),
                description=html_to_text(body),
                posted_at=parse_date(first(rec.get("pubdate"), rec.get("published"), rec.get("date"),
                                           rec.get("updated"))),
                employment_types=as_list(_field(rec, ["job_type", "jobtype"]) or None),
                tags=list(dict.fromkeys(cats + as_list(spec.get("category")))),
                raw={"feed": url},
            )
            if want_detail and ctx.wants_detail(title) and ctx.is_known(ctx.spec.id, post.source_job_id):
                continue  # stored with its detail already; a feed-level re-yield would churn versions
            if want_detail and not detail_blocked and details < max_details and ctx.wants_detail(title):
                details += 1
                try:
                    page = ctx.http.get(link)
                    if page.status_code == 200 and not merge_jobposting(post, page.text):
                        ctx.notes.append(f"no JobPosting JSON-LD: {link}")
                except Blocked as exc:
                    detail_blocked = True
                    ctx.notes.append(f"detail pages blocked ({exc.reason} {exc.status}); list-only from now on")
                except BudgetExceeded:
                    detail_blocked = True
            yield post
    if refused is not None and not any_feed:
        raise refused
