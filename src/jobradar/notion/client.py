"""Notion Jobs database: one page per shortlisted job; the Status the user sets there is read back each night.

Plain httpx against a pinned API version (2026-03-11): databases hold "data sources"; pages are created under a
data source and queried through it, so ``NOTION_JOBS_DB`` is a *data source* id (printed by ``jr notion init``).
~3 requests/s; 429 → wait Retry-After. Rich text ≤2,000 chars per object, ≤100 blocks per request.
Deleting = ``in_trash: true`` (Notion keeps it 30 days).
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import date, datetime
from typing import Any, Iterator

import httpx

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
TEXT_MAX = 2000
BODY_BLOCKS_MAX = 90
MIN_INTERVAL = 0.34
COPIES_MAX = 10

# Notion Status → decision status (local.db / state.db `decisions`)
STATUS_TO_DECISION = {"New": "new", "Apply": "picked", "Later": "later", "Skip": "skipped",
                      "Not relevant": "skipped", "Applied": "applied", "Screening": "screening",
                      "Interview": "interview", "Offer": "offer", "Rejected": "rejected"}
DECISION_TO_STATUS = {"new": "New", "picked": "Apply", "later": "Later", "skipped": "Skip", "applied": "Applied",
                      "screening": "Screening", "interview": "Interview", "offer": "Offer", "rejected": "Rejected"}
NOT_RELEVANT = "Not relevant"
JOB_STATUSES = list(STATUS_TO_DECISION)
STATUS_COLORS = {"New": "blue", "Apply": "green", "Later": "yellow", "Skip": "gray", "Not relevant": "brown",
                 "Applied": "purple", "Screening": "orange", "Interview": "orange", "Offer": "pink",
                 "Rejected": "red"}
SECTION_LABELS = {"top": "Top pick", "remote": "Remote", "home": "Near home", "elsewhere": "Elsewhere",
                  "review": "Review"}
WORK_SETUP_LABELS = {"remote": "Remote", "home": "Near home", "elsewhere": "Elsewhere", "unclear": "Not stated"}

# property name -> (type, select options); select values not listed are created by Notion on first use
JOBS_SCHEMA: dict[str, tuple[str, list[str] | None]] = {
    "Job": ("title", None),
    "Job ID": ("rich_text", None),
    "Status": ("select", JOB_STATUSES),
    "Fit": ("number", None),
    "Summary": ("rich_text", None),
    "Watch-outs": ("rich_text", None),
    "Company": ("rich_text", None),
    "Location": ("rich_text", None),
    "Work setup": ("select", list(WORK_SETUP_LABELS.values())),
    "Salary": ("rich_text", None),
    "Languages": ("rich_text", None),
    "Experience": ("rich_text", None),
    "Contract": ("rich_text", None),
    "Deadline": ("rich_text", None),
    "Section": ("select", list(SECTION_LABELS.values())),
    "Role": ("select", None),
    "Source": ("rich_text", None),
    "Posting URL": ("url", None),
    "Apply URL": ("url", None),
    "Added": ("date", None),
    "Posted": ("date", None),
    "Reviewed by": ("rich_text", None),
    "Same company": ("rich_text", None),
    "Applied on": ("date", None),
    "Notes": ("rich_text", None),
}

VIEWS_HELP = """Notion can't create views through its API. Add these to the Jobs database by hand:
  To review   : filter Status = New;      sort Fit descending, then Added descending
  Apply queue : filter Status = Apply
  In progress : filter Status is any of Applied, Screening, Interview, Offer
  Board       : Board layout, group by Status"""


class NotionError(RuntimeError):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(f"Notion {status} {code}: {message[:200]}")
        self.status, self.code = status, code


# ------------------------------------------------------------------ value helpers
def rich(text: str | None) -> list[dict[str, Any]]:
    text = text or ""
    return [{"type": "text", "text": {"content": text[i:i + TEXT_MAX]}} for i in range(0, len(text), TEXT_MAX)][:100]


def prop_value(ptype: str, value: Any) -> dict[str, Any]:
    if ptype == "title":
        return {"title": rich(str(value or ""))}
    if ptype == "rich_text":
        return {"rich_text": rich("" if value is None else str(value))}
    if ptype == "select":
        return {"select": {"name": str(value).replace(",", " ")[:100]} if value else None}
    if ptype == "number":
        return {"number": value}
    if ptype == "url":
        return {"url": value or None}
    if ptype == "date":
        if isinstance(value, (date, datetime)):
            value = value.isoformat()
        return {"date": {"start": str(value)[:10]} if value else None}
    raise ValueError(ptype)


def build_props(values: dict[str, Any], schema: dict[str, tuple[str, list[str] | None]] | None = None) -> dict[str, Any]:
    schema = schema or JOBS_SCHEMA
    return {name: prop_value(schema[name][0], v) for name, v in values.items() if name in schema}


def plain_value(p: dict[str, Any]) -> Any:
    t = p.get("type")
    v = p.get(t)
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text") or (x.get("text") or {}).get("content", "") for x in v or [])
    if t == "select":
        return (v or {}).get("name")
    if t == "date":
        return (v or {}).get("start")
    return v


def plain_page(page: dict[str, Any]) -> dict[str, Any]:
    out = {name: plain_value(p) for name, p in (page.get("properties") or {}).items()}
    out["_id"] = page.get("id")
    out["_url"] = page.get("url")
    out["_edited"] = page.get("last_edited_time")
    return out


def paragraph(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich(text)}}


def heading(text: str, level: int = 2) -> dict[str, Any]:
    key = f"heading_{level}"
    return {"object": "block", "type": key, key: {"rich_text": rich(text)}}


def callout(text: str, emoji: str = "⚠️") -> dict[str, Any]:
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": rich(text), "icon": {"type": "emoji", "emoji": emoji}, "color": "red_background"}}


def bullet(text: str, url: str | None = None) -> dict[str, Any]:
    rt = rich(text)
    if url and rt:
        rt[0]["text"]["link"] = {"url": url}
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rt}}


def text_blocks(text: str, limit: int) -> list[dict[str, Any]]:
    """Paragraph blocks of ≤2,000 chars, split on paragraph boundaries where possible (at most limit + 1)."""
    blocks: list[dict[str, Any]] = []
    buf = ""
    for para in [p.strip() for p in (text or "").split("\n") if p.strip()]:
        while len(para) > TEXT_MAX:
            if buf:
                blocks.append(paragraph(buf))
                buf = ""
            cut = para.rfind(" ", 0, TEXT_MAX)
            cut = cut if cut > TEXT_MAX // 2 else TEXT_MAX
            blocks.append(paragraph(para[:cut]))
            para = para[cut:].strip()
        if buf and len(buf) + 1 + len(para) > TEXT_MAX:
            blocks.append(paragraph(buf))
            buf = ""
        buf = f"{buf}\n{para}" if buf else para
    if buf:
        blocks.append(paragraph(buf))
    return blocks[:limit + 1]


def job_body(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Page body: instructions to applicants, other listings of the same job, then the posting text."""
    blocks: list[dict[str, Any]] = []
    if row.get("instructions"):
        blocks.append(callout("Instructions in the posting: " + " · ".join(row["instructions"])))
    copies = row.get("copies") or []
    if copies:
        blocks.append(heading(f"Also listed ({len(copies)})", 3))
        for c in copies[:COPIES_MAX]:
            blocks.append(bullet(" · ".join(x for x in (c.get("title"), c.get("company"), c.get("location"),
                                                        c.get("source")) if x), c.get("url")))
        if len(copies) > COPIES_MAX:
            blocks.append(paragraph(f"…and {len(copies) - COPIES_MAX} more"))
    blocks.append(heading("Posting"))
    room = BODY_BLOCKS_MAX - len(blocks) - 1
    posting = text_blocks(row.get("description") or "", room)
    if len(posting) > room:
        posting = posting[:room] + [paragraph(f"(shortened) Full posting: {row.get('posting_url') or ''}")]
    if not posting:
        posting = [paragraph(f"No description: open the posting. {row.get('posting_url') or ''}")]
    return blocks + posting


def review_values(row: dict[str, Any]) -> dict[str, Any]:
    """The properties the overnight review fills (set again when a job is reviewed on a later night)."""
    watch = [f"⚠ {i}" for i in row.get("instructions") or []] + list(row.get("watch_out") or [])
    return {"Fit": row.get("fit"), "Summary": row.get("summary") or "", "Watch-outs": "; ".join(watch),
            "Work setup": WORK_SETUP_LABELS.get(row.get("work_setup") or ""),
            "Salary": row.get("screen_salary") or row.get("salary") or "",
            "Languages": row.get("languages_required") or "", "Experience": row.get("years_required") or "",
            "Contract": row.get("contract") or "", "Deadline": row.get("apply_deadline") or "",
            "Reviewed by": row.get("screen_model") or ""}


def job_values(row: dict[str, Any], status: str = "New") -> dict[str, Any]:
    sources = row.get("sources") or []
    return {
        "Job": f"{row.get('title', '')} — {row.get('company', '')}",
        "Job ID": row["job_id"],
        "Status": status,
        "Company": row.get("company") or "",
        "Location": row.get("location_display") or "",
        "Section": SECTION_LABELS.get(row.get("section") or "", row.get("section")),
        "Role": row.get("family_label") or row.get("family"),
        "Source": ", ".join(sources) if isinstance(sources, list) else str(sources),
        "Posting URL": row.get("posting_url") or row.get("url"),
        "Apply URL": row.get("route_url") or row.get("url"),
        "Added": row.get("night") or date.today().isoformat(),
        "Posted": row.get("posted_at"),
        "Same company": row.get("same_company") or "",
        **review_values(row),
    }


def schema_props(schema: dict[str, tuple[str, list[str] | None]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, (ptype, options) in schema.items():
        cfg: dict[str, Any] = {}
        if ptype == "select":
            cfg = {"options": [{"name": o, "color": STATUS_COLORS.get(o, "default")} for o in options or []]}
        elif ptype == "number":
            cfg = {"format": "number"}
        out[name] = {"type": ptype, ptype: cfg}
    return out


def page_id_from(text: str) -> str:
    """A page id from a Notion link or a bare id (the last 32 hex characters)."""
    hexes = re.findall(r"[0-9a-f]{32}|[0-9a-f-]{36}", text.lower())
    if not hexes:
        raise NotionError(0, "config", "no page id in that link")
    return hexes[-1].replace("-", "")


# ------------------------------------------------------------------------ client
class NotionClient:
    def __init__(self, token: str | None = None, *, jobs_ds: str | None = None,
                 transport: httpx.BaseTransport | None = None, sleep: bool = True):
        token = token or os.environ.get("NOTION_TOKEN")
        if not token:
            raise NotionError(0, "config", "NOTION_TOKEN is not set")
        self.jobs_ds = jobs_ds or os.environ.get("NOTION_JOBS_DB")
        self.sleep = sleep
        self.requests = 0
        self._next_ok = 0.0
        self._http = httpx.Client(base_url=API, timeout=60.0, transport=transport, headers={
            "Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"})

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "NotionClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _wait(self, seconds: float) -> None:
        if self.sleep and seconds > 0:
            time.sleep(seconds)

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, *,
                retry_unsafe: bool = True) -> dict[str, Any]:
        """``retry_unsafe=False`` (page creation): no retry after a timeout/5xx, where the write may have landed;
        429 is always retried (the request was refused, not processed)."""
        for attempt in range(5):
            now = time.monotonic()
            self._wait(self._next_ok - now)
            self._next_ok = max(now, self._next_ok) + MIN_INTERVAL
            self.requests += 1
            try:
                resp = self._http.request(method, path, json=body)
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt >= 2 or not retry_unsafe:
                    raise
                self._wait(2 ** attempt)
                continue
            if resp.status_code == 429:
                try:
                    delay = float(resp.headers.get("Retry-After", "1"))
                except ValueError:
                    delay = 1.0
                self._wait(min(delay, 60.0))
                continue
            if resp.status_code >= 500 and attempt < 2 and retry_unsafe:
                self._wait(2 ** attempt)
                continue
            if resp.status_code >= 400:
                try:
                    err = resp.json()
                except ValueError:
                    err = {}
                raise NotionError(resp.status_code, err.get("code", ""), err.get("message", ""))
            return resp.json()
        raise NotionError(429, "rate_limited", "gave up after repeated 429s")

    def query(self, ds: str, filter: dict[str, Any] | None = None,
              sorts: list[dict[str, Any]] | None = None) -> Iterator[dict[str, Any]]:
        body: dict[str, Any] = {"page_size": 100}
        if filter:
            body["filter"] = filter
        if sorts:
            body["sorts"] = sorts
        while True:
            res = self.request("POST", f"/data_sources/{ds}/query", body)
            yield from res.get("results") or []
            if not res.get("has_more") or not res.get("next_cursor"):
                return
            body["start_cursor"] = res["next_cursor"]

    def need_jobs(self) -> str:
        if not self.jobs_ds:
            raise NotionError(0, "config", "NOTION_JOBS_DB is not set (run `jr notion init`)")
        return self.jobs_ds

    # -- setup -----------------------------------------------------------------
    def init_jobs(self, parent: str) -> dict[str, str]:
        """Create the Jobs database under the page the integration was given access to."""
        db = self.request("POST", "/databases", {
            "parent": {"type": "page_id", "page_id": page_id_from(parent)}, "title": rich("Jobs"),
            "initial_data_source": {"properties": schema_props(JOBS_SCHEMA)}})
        sources = db.get("data_sources") or self.request("GET", f"/databases/{db['id']}").get("data_sources") or []
        self.jobs_ds = sources[0]["id"]
        return {"database": db["id"], "data_source": self.jobs_ds, "url": db.get("url") or ""}

    def ensure_jobs_schema(self) -> list[str]:
        """Add properties `JOBS_SCHEMA` has and the live database lacks (a newer package version)."""
        ds = self.need_jobs()
        have = self.request("GET", f"/data_sources/{ds}").get("properties") or {}
        missing = {k: v for k, v in JOBS_SCHEMA.items() if k not in have}
        if missing:
            self.request("PATCH", f"/data_sources/{ds}", {"properties": schema_props(missing)})
        return sorted(missing)

    # -- pages -------------------------------------------------------------------
    def create_job(self, row: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {"parent": {"type": "data_source_id", "data_source_id": self.need_jobs()},
                                "properties": build_props(job_values(row)), "children": job_body(row)[:100]}
        return self.request("POST", "/pages", body, retry_unsafe=False)

    def set_values(self, page_id: str, values: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", f"/pages/{page_id}", {"properties": build_props(values)})

    def trash(self, page_id: str) -> None:
        self.request("PATCH", f"/pages/{page_id}", {"in_trash": True})

    def changed_since(self, since: datetime) -> list[dict[str, Any]]:
        """Pages whose Status the user moved off New, edited on or after `since`."""
        flt = {"and": [{"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": since.isoformat()}},
                       {"property": "Status", "select": {"does_not_equal": "New"}}]}
        return [plain_page(p) for p in self.query(self.need_jobs(), flt)]

    def job_page(self, job_id: str) -> dict[str, Any] | None:
        hit = next(iter(self.query(self.need_jobs(), {"property": "Job ID", "rich_text": {"equals": job_id}})), None)
        return plain_page(hit) if hit else None
