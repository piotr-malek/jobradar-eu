"""Core data model shared by adapters, matcher, store and digest."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def short_hash(*parts: str, n: int = 20) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:n]


@dataclass
class Posting:
    """One job posting as returned by one source, normalized to plain fields.

    Adapters fill what the source provides; anything unknown stays empty and the
    normalizers/matcher infer it from text.
    """

    source: str
    source_job_id: str
    title: str
    company: str
    url: str
    apply_url: str | None = None
    location_raw: str = ""
    countries: list[str] = field(default_factory=list)  # ISO-3166 alpha-2 from structured data
    cities: list[str] = field(default_factory=list)
    workplace: str | None = None  # remote | hybrid | onsite (structured only)
    remote_scope: str | None = None  # e.g. "Europe", "USA Only", "CET +/- 3h"
    description: str = ""  # plain text
    posted_at: datetime | None = None
    employment_types: list[str] = field(default_factory=list)
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None  # year | month | day | hour
    tags: list[str] = field(default_factory=list)
    ats_type: str | None = None
    ats_slug: str | None = None
    ats_job_id: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def posting_key(self) -> str:
        return short_hash(self.source, str(self.source_job_id))

    def version_hash(self) -> str:
        payload = json.dumps(
            [
                self.title,
                self.company,
                self.location_raw,
                sorted(self.countries),
                self.workplace,
                self.remote_scope,
                self.description[:6000],
                self.salary_min,
                self.salary_max,
                self.apply_url or self.url,
            ],
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["posting_key"] = self.posting_key
        return d


@dataclass
class MatchResult:
    posting_key: str
    rule_version: str
    tier: str  # core | adjacent | review | excluded
    family: str | None
    section: str | None  # remote | hybrid_pl | hybrid_eu (None when excluded)
    decision: str  # match | drop
    drop_reason: str | None
    work_mode: str  # remote | hybrid | onsite | unknown
    countries: list[str]
    flags: list[str]
    stack_hits: list[str]
    seniority: str | None
    contract: list[str]
    posting_lang: str | None
    priority: float
    explain: dict[str, Any]
