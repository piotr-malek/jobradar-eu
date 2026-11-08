"""Source framework: spec (from defaults/sources.yaml + user/sources.yaml), fetch context, adapter registry, helpers."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable, Iterator

from jobradar.http.client import PoliteClient
from jobradar.models import Posting

log = logging.getLogger(__name__)


@dataclass
class SourceSpec:
    id: str
    adapter: str
    kind: str = "board"  # board | ats | aggregator | national
    enabled: bool = True
    every_days: int = 1
    runner: str = "github"  # github | local | any
    rank: int = 50  # lower = preferred representative link in dedup
    params: dict[str, Any] = field(default_factory=dict)
    politeness: dict[str, Any] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    default_country: str | None = None
    default_workplace: str | None = None
    track: str | None = None  # "purpose" → postings tagged "purpose" and matched with the purpose rules
    notes: str = ""
    countries: list[str] = field(default_factory=lambda: ["EU"])  # ISO codes it covers; EU = pan-European/remote
    langs: list[str] = field(default_factory=list)  # posting languages besides English ($terms adds their terms)
    opt_in: str | None = None  # "purpose" | "polish_only" | "local_ip": runs only when the user switched the track on
    label: str = ""  # shown in the wizard
    packs: list[str] = field(default_factory=list)  # role-specific board: runs only when one of these packs is active
    modes: list[str] = field(default_factory=list)  # lite | full: runs only in these modes (empty = both)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SourceSpec":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


@dataclass
class FetchContext:
    spec: SourceSpec
    http: PoliteClient
    wants_detail: Callable[[str], bool] = lambda title: True
    known: dict[str, str] = field(default_factory=dict)  # posting_key -> version_hash already stored
    secrets: dict[str, str] = field(default_factory=dict)
    log: logging.Logger = log
    notes: list[str] = field(default_factory=list)
    seen_known: list[str] = field(default_factory=list)  # posting_keys skipped because already stored
    skipped: int = 0  # records read but deliberately not yielded (title pre-filter); count toward fetched

    @property
    def params(self) -> dict[str, Any]:
        return self.spec.params

    def is_known(self, source: str, source_job_id: str) -> bool:
        from jobradar.models import short_hash

        return short_hash(source, str(source_job_id)) in self.known

    def mark_seen(self, source: str, source_job_id: str) -> None:
        """Record a sighting for a stored posting the adapter chose not to re-fetch (keeps last_seen fresh)."""
        from jobradar.models import short_hash

        pk = short_hash(source, str(source_job_id))
        if pk in self.known:
            self.seen_known.append(pk)


def default_terms(n: int | None = None, langs: list[str] | None = None) -> list[str]:
    """Search words of the active role packs: the fallback when a source's yaml entry passes no queries."""
    from jobradar import config

    return config.search_terms(langs, n)


AdapterFn = Callable[[FetchContext], Iterable[Posting]]
ADAPTERS: dict[str, AdapterFn] = {}


def adapter(name: str) -> Callable[[AdapterFn], AdapterFn]:
    def deco(fn: AdapterFn) -> AdapterFn:
        ADAPTERS[name] = fn
        return fn

    return deco


# ------------------------------------------------------------------ helpers
_REL = re.compile(r"(\d+)\+?\s*(minute|hour|day|week|month)s?\s+ago", re.I)


def parse_date(value: Any) -> datetime | None:
    """ISO strings, epoch s/ms, RFC 2822, 'Posted 3 days ago', 'today', 'yesterday'."""
    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if s.isdigit():
        return parse_date(int(s))
    low = s.lower()
    now = datetime.now(timezone.utc)
    if "today" in low or "just posted" in low or "dzisiaj" in low:
        return now
    if "yesterday" in low or "wczoraj" in low:
        return now - timedelta(days=1)
    m = _REL.search(low)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        mult = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30}[unit]
        return now - timedelta(days=n * mult)
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[: len(fmt) + 4], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        f = float(str(value).replace(",", "").replace(" ", ""))
        return f if f > 0 else None
    except ValueError:
        return None


def first(*values: Any) -> Any:
    for v in values:
        if v not in (None, "", [], {}):
            return v
    return None


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def take(it: Iterable[Posting], limit: int | None) -> Iterator[Posting]:
    for i, p in enumerate(it):
        if limit is not None and i >= limit:
            return
        yield p
