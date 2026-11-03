"""Cross-source dedup: ats_key / fz_key + union-find clustering."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from jobradar.models import short_hash
from jobradar.normalize.names import company_key, title_key

# vendor, regex with named groups slug + job (job optional)
ATS_URL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?(?P<slug>[\w-]+)(?:/jobs/(?P<job>\d+))?", re.I)),
    ("greenhouse", re.compile(r"[?&]gh_jid=(?P<job>\d+)", re.I)),
    ("lever", re.compile(r"jobs(?:\.eu)?\.lever\.co/(?P<slug>[\w.-]+)(?:/(?P<job>[0-9a-f-]{36}))?", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/(?P<slug>[\w.%-]+)(?:/(?P<job>[0-9a-f-]{36}))?", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/(?P<slug>[\w-]+)(?:/j/(?P<job>[0-9A-F]+))?", re.I)),
    ("recruitee", re.compile(r"(?P<slug>[\w-]+)\.recruitee\.com(?:/o/(?P<job>[\w-]+))?", re.I)),
    ("personio", re.compile(r"(?P<slug>[\w-]+)\.jobs\.personio\.(?:de|com)(?:/job/(?P<job>\d+))?", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/(?P<slug>[\w-]+)(?:/(?P<job>\d+))?", re.I)),
    ("teamtailor", re.compile(r"(?P<slug>[\w-]+)\.teamtailor\.com(?:/jobs/(?P<job>\d+))?", re.I)),
    ("bamboohr", re.compile(r"(?P<slug>[\w-]+)\.bamboohr\.com/(?:careers|jobs)(?:/(?:view\.php\?id=)?(?P<job>\d+))?", re.I)),
    ("breezy", re.compile(r"(?P<slug>[\w-]+)\.breezy\.hr(?:/p/(?P<job>[\w-]+))?", re.I)),
    ("workday", re.compile(r"(?P<slug>[\w-]+)\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?[\w-]+/job/[^?#]*?_(?P<job>[\w-]+?)(?:-\d+)?(?:[/?#]|$)", re.I)),
]

# Lower = better representative link (direct employer ATS first, aggregators last).
SOURCE_RANK_DEFAULT = 50


def parse_ats(url: str | None) -> tuple[str, str, str | None] | None:
    if not url:
        return None
    for vendor, rx in ATS_URL_PATTERNS:
        m = rx.search(url)
        if m:
            slug = (m.groupdict().get("slug") or "").lower()
            job = m.groupdict().get("job")
            if vendor == "greenhouse" and not slug and job:
                return vendor, "", job
            if slug in {"www", "api", "jobs", "careers", "apply", "boards", "job-boards", "embed"}:
                continue
            return vendor, slug, job
    return None


def ats_key(url: str | None, ats_type: str | None = None, ats_slug: str | None = None, ats_job_id: str | None = None) -> str | None:
    if ats_type and ats_slug and ats_job_id:
        return f"ats:{ats_type}:{ats_slug.lower()}:{ats_job_id}"
    parsed = parse_ats(url)
    if parsed and parsed[1] and parsed[2]:
        return f"ats:{parsed[0]}:{parsed[1]}:{parsed[2]}"
    return None


def fz_key(company: str | None, title: str | None) -> str | None:
    c, t = company_key(company), title_key(title)
    if not c or not t:
        return None
    return f"{c}|{t}"


@dataclass
class Cluster:
    cluster_id: str
    members: list[dict[str, Any]]
    representative: dict[str, Any]
    keys: set[str] = field(default_factory=set)

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for m in sorted(self.members, key=lambda r: r.get("_rank", SOURCE_RANK_DEFAULT)):
            if m["source"] not in seen:
                seen.append(m["source"])
        return seen


class _UF:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def dedup_keys(row: dict[str, Any]) -> set[str]:
    keys = {f"pk:{row['posting_key']}"}
    ak = row.get("ats_key") or ats_key(row.get("apply_url") or row.get("url"), row.get("ats_type"),
                                       row.get("ats_slug"), row.get("ats_job_id"))
    if ak:
        keys.add(ak)
    fk = fz_key(row.get("company"), row.get("title"))     # recomputed: the stored fz_key predates rule fixes
    if fk:
        keys.add(f"fz:{fk}")
    return keys


def cluster_rows(rows: Iterable[dict[str, Any]], source_rank: dict[str, int] | None = None) -> list[Cluster]:
    """Union rows sharing any dedup key: posting key, ATS job, or company + title (`fz_key`). One company's listings
    with the same title are one job, also on one board (Sii posts a copy per city) and for agencies (Hays may post
    several clients' roles under one title: the shortlist row lists every copy's link, so none is lost)."""
    rows = list(rows)
    source_rank = source_rank or {}
    uf = _UF()
    key_owner: dict[str, str] = {}
    row_keys: dict[str, set[str]] = {}
    for r in rows:
        pk = r["posting_key"]
        r["_rank"] = source_rank.get(r["source"], SOURCE_RANK_DEFAULT)
        ks = dedup_keys(r)
        row_keys[pk] = ks
        uf.find(pk)
        for k in ks:
            if k in key_owner:
                uf.union(pk, key_owner[k])
            else:
                key_owner[k] = pk
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(uf.find(r["posting_key"]), []).append(r)
    clusters: list[Cluster] = []
    for members in groups.values():
        rep = sorted(
            members,
            key=lambda r: (r["_rank"], -(r.get("priority") or 0), str(r.get("first_seen_at") or "")),
        )[0]
        keys: set[str] = set()
        for m in members:
            keys |= row_keys[m["posting_key"]]
        cid = short_hash(*sorted(k for k in keys if not k.startswith("pk:")) or [rep["posting_key"]], n=12)
        clusters.append(Cluster(cluster_id=cid, members=members, representative=rep, keys=keys))
    return clusters


_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def short_job_id(seed: str) -> str:
    """Stable 6-char human id for the digest / Sheet, e.g. JR-7KQ2M9."""
    n = int(short_hash(seed, n=12), 16)
    out = ""
    for _ in range(6):
        out = _B32[n % 32] + out
        n //= 32
    return f"JR-{out}"
