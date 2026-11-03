"""SQLite store: the overnight state (state.db) that the workflow keeps on the config repo's `state` branch.

Same tables as before (store/schema.py), mostly append-only; list columns are JSON text, timestamps ISO-8601 UTC
text. One writer at a time (the workflow, or `jr` on the laptop); the app opens a read-only copy.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from jobradar.store.base import shape_row
from jobradar.store.schema import TABLES

OK_STATUSES = ("success", "partial")
SQL_TYPES = {"STRING": "TEXT", "INT64": "INTEGER", "FLOAT64": "REAL", "BOOL": "INTEGER", "TIMESTAMP": "TEXT",
             "DATE": "TEXT"}
INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_pv_key ON posting_versions(posting_key, observed_at)",
    "CREATE INDEX IF NOT EXISTS ix_pv_source ON posting_versions(source, posting_key)",
    "CREATE INDEX IF NOT EXISTS ix_pv_observed ON posting_versions(observed_at)",
    "CREATE INDEX IF NOT EXISTS ix_ps_key ON posting_sightings(posting_key, seen_at)",
    "CREATE INDEX IF NOT EXISTS ix_m_rv ON matches(rule_version, posting_key, matched_at)",
    "CREATE INDEX IF NOT EXISTS ix_m_key ON matches(posting_key, matched_at)",
    "CREATE INDEX IF NOT EXISTS ix_sr_source ON source_runs(source_id, started_at)",
    "CREATE INDEX IF NOT EXISTS ix_sl_job ON shortlist(job_id)",
    "CREATE INDEX IF NOT EXISTS ix_sl_night ON shortlist(night)",
    "CREATE INDEX IF NOT EXISTS ix_dec_job ON decisions(job_id, at)",
    "CREATE INDEX IF NOT EXISTS ix_apr ON ats_poll_results(ats_type, ats_slug, polled_at)",
    "CREATE INDEX IF NOT EXISTS ix_reg ON ats_registry(ats_type, ats_slug)",
]
VIEWS = {
    "v_postings_current": """
        SELECT l.*, s.first_seen_at, COALESCE(g.last_seen_at, s.first_seen_at) AS last_seen_at FROM
          (SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY posting_key ORDER BY observed_at DESC) AS rn
                          FROM posting_versions) WHERE rn = 1) l
          JOIN (SELECT posting_key, MIN(observed_at) AS first_seen_at FROM posting_versions GROUP BY posting_key) s
            USING (posting_key)
          LEFT JOIN (SELECT posting_key, MAX(seen_at) AS last_seen_at FROM posting_sightings GROUP BY posting_key) g
            USING (posting_key)""",
    "v_matches_current": """
        SELECT * FROM (SELECT m.*, ROW_NUMBER() OVER (PARTITION BY m.posting_key ORDER BY m.matched_at DESC) AS rn
                       FROM matches m
                       WHERE m.rule_version = COALESCE(
                         (SELECT rule_version FROM rule_versions ORDER BY created_at DESC LIMIT 1),
                         (SELECT rule_version FROM matches ORDER BY matched_at DESC LIMIT 1))) WHERE rn = 1""",
    "v_drop_audit": """
        SELECT m.posting_key, m.source, m.title, m.company, m.drop_reason, m.matched_at, m.rule_version
        FROM v_matches_current m WHERE m.decision = 'drop'""",
    "v_source_health": """
        SELECT source_id, COUNT(*) AS runs, SUM(status IN ('success', 'partial')) AS ok_runs,
               MAX(started_at) AS last_run, MAX(CASE WHEN status IN ('success', 'partial') THEN started_at END) AS last_ok,
               SUM(records_fetched) AS records, SUM(matched) AS matched, SUM(requests) AS requests
        FROM source_runs WHERE started_at >= datetime('now', '-14 days') GROUP BY source_id""",
    "v_decisions_current": """
        SELECT * FROM (SELECT d.*, ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY at DESC) AS rn FROM decisions d)
        WHERE rn = 1""",
    "v_job_journey": """
        SELECT s.job_id, s.title, s.company, MIN(s.night) AS shortlisted_on, d.status, d.at AS status_at, s.sources,
               s.family, s.fit
        FROM shortlist s LEFT JOIN v_decisions_current d USING (job_id) GROUP BY s.job_id""",
}


def _iso(v: Any) -> Any:
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


def _dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class SQLiteStore:
    """state.db. ``read_only=True`` opens it with ``mode=ro`` (the app's pulled copy)."""

    def __init__(self, path: Path | str, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self._local = threading.local()
        self._schema_lock = threading.Lock()
        self._schema_done = False
        self._repeated = {t: {f for f, _, mode in spec[0] if mode == "REPEATED"} for t, spec in TABLES.items()}

    # ------------------------------------------------------------- plumbing
    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            if self.read_only:
                c = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                c = sqlite3.connect(self.path, check_same_thread=False, timeout=60)
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA synchronous=NORMAL")
            c.row_factory = sqlite3.Row
            self._local.conn = c
            if not self.read_only:
                with self._schema_lock:
                    if not self._schema_done:
                        self._ensure_schema(c)
                        self._schema_done = True
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            if not self.read_only:
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.close()
            self._local.conn = None

    def _ensure_schema(self, c: sqlite3.Connection) -> None:
        for table, (fields, *_rest) in TABLES.items():
            cols = ", ".join(f'"{n}" {SQL_TYPES[t]}' for n, t, _m in fields)
            c.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({cols})')
            have = {r[1] for r in c.execute(f'PRAGMA table_info("{table}")')}
            for n, t, _m in fields:                       # new columns from a newer package version
                if n not in have:
                    c.execute(f'ALTER TABLE "{table}" ADD COLUMN "{n}" {SQL_TYPES[t]}')
        for ix in INDEXES:
            c.execute(ix)
        for name, body in VIEWS.items():
            c.execute(f"DROP VIEW IF EXISTS {name}")
            c.execute(f"CREATE VIEW {name} AS {body}")
        c.commit()

    def init(self) -> None:
        _ = self.conn

    def _decode(self, table: str | None, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        rep = self._repeated.get(table or "", set())
        for k, v in d.items():
            if isinstance(v, str) and (k in rep or (table is None and v[:1] == "[" and k in _ALL_REPEATED)):
                try:
                    d[k] = json.loads(v)
                except ValueError:
                    pass
        return d

    def query(self, sql: str, params: dict[str, Any] | Iterable[Any] | None = None, *,
              table: str | None = None) -> list[dict[str, Any]]:
        if isinstance(params, dict):
            params = {k: _iso(v) for k, v in params.items()}
        cur = self.conn.execute(sql, params or {})
        return [self._decode(table, r) for r in cur.fetchall()]

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> int:
        cur = self.conn.execute(sql, {k: _iso(v) for k, v in (params or {}).items()})
        self.conn.commit()
        return cur.rowcount

    def append(self, table: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        fields = [f for f, _t, _m in TABLES[table][0]]
        rep = self._repeated[table]
        data = []
        for r in rows:
            shaped = shape_row(table, r)
            data.append([json.dumps(shaped[f], ensure_ascii=False) if f in rep else
                         (int(shaped[f]) if isinstance(shaped[f], bool) else shaped[f]) for f in fields])
        cols = ", ".join(f'"{f}"' for f in fields)
        marks = ", ".join("?" for _ in fields)
        with self.conn:
            self.conn.executemany(f'INSERT INTO "{table}" ({cols}) VALUES ({marks})', data)
        return len(rows)

    def read(self, table: str) -> list[dict[str, Any]]:
        return self.query(f'SELECT * FROM "{table}"', table=table)

    # ------------------------------------------------------------- pipeline queries
    def known_versions(self, source: str) -> dict[str, str]:
        rows = self.conn.execute(
            """SELECT posting_key, version_hash FROM (
                 SELECT posting_key, version_hash,
                        ROW_NUMBER() OVER (PARTITION BY posting_key ORDER BY observed_at DESC) AS rn
                 FROM posting_versions WHERE source = ?) WHERE rn = 1""", (source,)).fetchall()
        return {r[0]: r[1] for r in rows}

    def last_runs(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        rows = self.conn.execute(
            "SELECT source_id, started_at, status, COALESCE(records_fetched, 0) FROM source_runs "
            "ORDER BY source_id, started_at DESC").fetchall()
        for sid, started, status, rec in rows:
            info = out.get(sid)
            if info is None:
                info = out[sid] = {"last_ok": None, "last_any": _dt(started), "last_status": status,
                                   "consecutive_fail": 0, "_done": False}
            if status in OK_STATUSES and info["last_ok"] is None:
                info["last_ok"] = _dt(started)
            if not info["_done"]:
                if status in OK_STATUSES and rec > 0:
                    info["_done"] = True
                else:
                    info["consecutive_fail"] += 1
        for info in out.values():
            info.pop("_done", None)
        return out

    def candidates(self, since: datetime, rule_version: str, lookback_days: int = 60) -> list[dict[str, Any]]:
        horizon = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        versions = self.query(
            """SELECT v.*, f.first_seen_at, COALESCE(l.last_seen_at, f.first_seen_at) AS last_seen_at FROM
                 (SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY posting_key ORDER BY observed_at DESC) AS rn
                                 FROM posting_versions WHERE observed_at >= :horizon) WHERE rn = 1) v
                 JOIN (SELECT posting_key, MIN(observed_at) AS first_seen_at FROM posting_versions
                       GROUP BY posting_key) f USING (posting_key)
                 LEFT JOIN (SELECT posting_key, MAX(seen_at) AS last_seen_at FROM posting_sightings
                            GROUP BY posting_key) l USING (posting_key)
               WHERE f.first_seen_at >= :since
                 AND v.posting_key IN (SELECT posting_key FROM matches WHERE rule_version = :rv AND decision = 'match')""",
            {"horizon": horizon, "since": since, "rv": rule_version}, table="posting_versions")
        if not versions:
            return []
        matches = {m["posting_key"]: m for m in self.query(
            """SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY posting_key ORDER BY matched_at DESC) AS rn
                              FROM matches WHERE rule_version = :rv) WHERE rn = 1""", {"rv": rule_version},
            table="matches")}
        out = []
        for v in versions:
            m = matches.get(v["posting_key"])
            if not m or m["decision"] != "match" or m.get("version_hash") not in (None, v["version_hash"]):
                continue
            row = {**v, **{k: m[k] for k in m if k not in ("posting_key", "version_hash", "title", "company", "source",
                                                         "rn")}}
            row.pop("rn", None)
            out.append(row)
        return out

    def sent_dedup_keys(self, days: int = 120) -> set[str]:
        """Keys of jobs already shortlisted (or decided on) in the last `days`: never shown again."""
        from jobradar.dedup.cluster import fz_key

        cutoff = (date.today() - timedelta(days=days)).isoformat()
        keys: set[str] = set()
        for table, col in (("shortlist", "night"), ("decisions", "at")):
            for r in self.conn.execute(f"SELECT dedup_keys, company, title, job_id FROM {table} WHERE {col} >= ?",
                                       (cutoff,)):
                keys.update(json.loads(r[0] or "[]"))
                keys.add(f"job:{r[3]}")
                if fk := fz_key(r[1], r[2]):
                    keys.add(f"fz:{fk}")
        return keys

    def shortlisted_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT DISTINCT job_id FROM shortlist")}

    def night_done(self, night: date, kind: str) -> bool:
        return bool(self.conn.execute(
            "SELECT COUNT(*) FROM nights WHERE night = ? AND kind = ? AND status = 'done'",
            (night.isoformat(), kind)).fetchone()[0])

    digest_sent = night_done            # the old name, used by a few callers

    def last_night_at(self) -> datetime | None:
        r = self.conn.execute("SELECT MAX(finished_at) FROM nights WHERE status = 'done'").fetchone()
        return _dt(r[0]) if r and r[0] else None

    last_digest_sent_at = last_night_at

    def latest_versions(self, since: datetime) -> list[dict[str, Any]]:
        return self.query(
            """SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY posting_key ORDER BY observed_at DESC) AS rn
                              FROM posting_versions WHERE observed_at >= :since) WHERE rn = 1""",
            {"since": since}, table="posting_versions")

    def rule_version_known(self, rule_version: str) -> bool:
        return bool(self.conn.execute("SELECT COUNT(*) FROM rule_versions WHERE rule_version = ?",
                                      (rule_version,)).fetchone()[0])

    def ats_registry(self) -> list[dict[str, Any]]:
        rows = self.query(
            """SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY ats_type, ats_slug
                                                          ORDER BY discovered_at DESC, rowid DESC) AS rn
                              FROM ats_registry) WHERE rn = 1""", table="ats_registry")
        for r in rows:
            r.pop("rn", None)
        return rows

    def ats_poll_summary(self) -> dict[tuple[str, str], dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {}
        rows = self.conn.execute(
            "SELECT ats_type, ats_slug, polled_at, http_status, n_eu, n_matched FROM ats_poll_results "
            "ORDER BY ats_type, ats_slug, polled_at").fetchall()
        for vendor, slug, polled, status, n_eu, n_matched in rows:
            s = out.setdefault((vendor, slug), {"last_polled_at": None, "consecutive_404": 0, "max_eu": 0,
                                                "max_matched": 0, "last_status": None, "ok_polls": 0})
            s["last_polled_at"] = _dt(polled)
            s["last_status"] = status
            s["consecutive_404"] = s["consecutive_404"] + 1 if status == 404 else 0
            s["ok_polls"] += status == 200
            s["max_eu"] = max(s["max_eu"], n_eu or 0)
            s["max_matched"] = max(s["max_matched"], n_matched or 0)
        return out

    def shortlist_since(self, since: date) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM shortlist WHERE night >= :since", {"since": since}, table="shortlist")

    digest_items_since = shortlist_since

    def source_health(self, days: int = 14) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return self.query("SELECT * FROM source_runs WHERE started_at >= :c", {"c": cutoff}, table="source_runs")

    def funnel_stats(self, since: datetime) -> dict[str, Any]:
        r = self.conn.execute(
            """WITH p AS (SELECT posting_key, MIN(observed_at) AS first_seen FROM posting_versions GROUP BY posting_key),
                    m AS (SELECT DISTINCT posting_key FROM matches WHERE decision = 'match'),
                    d AS (SELECT job_id, MIN(night) AS first_night FROM shortlist GROUP BY job_id)
               SELECT (SELECT MIN(first_seen) FROM p), (SELECT COUNT(*) FROM p),
                      (SELECT COUNT(*) FROM p WHERE first_seen >= :since), (SELECT COUNT(*) FROM m),
                      (SELECT COUNT(*) FROM m JOIN p USING (posting_key) WHERE first_seen >= :since),
                      (SELECT COUNT(*) FROM d), (SELECT COUNT(*) FROM d WHERE first_night >= :since_date)""",
            {"since": _iso(since), "since_date": since.date().isoformat()}).fetchone()
        return {"first_seen": _dt(r[0]), "postings": r[1], "postings_new": r[2], "matched": r[3],
                "matched_new": r[4], "delivered": r[5], "delivered_new": r[6]}

    def drop_counts(self, since: datetime, rule_version: str) -> list[tuple[str, int]]:
        from collections import Counter

        c: Counter[str] = Counter()
        for (reason,) in self.conn.execute(
                "SELECT drop_reason FROM matches WHERE rule_version = ? AND decision = 'drop' AND matched_at >= ?",
                (rule_version, _iso(since))):
            c[re.sub(r"\s*\(.*\)", "", reason or "")] += 1
        return c.most_common(12)

    def elsewhere_dropped(self, since: datetime, rule_version: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT DISTINCT company, title FROM matches WHERE rule_version = ? AND decision = 'drop'
               AND (drop_reason LIKE 'hybrid outside%' OR drop_reason LIKE 'onsite outside%'
                    OR drop_reason LIKE 'on-site only%') AND matched_at >= ?""",
            (rule_version, _iso(since))).fetchall()
        return [{"company": c, "title": t} for c, t in rows]

    def job_postings(self, job_ids: list[str]) -> list[dict[str, Any]]:
        want = set(job_ids)
        owners: dict[str, set[str]] = {}
        for r in self.conn.execute("SELECT job_id, member_posting_keys FROM shortlist"):
            if r[0] in want:
                for pk in json.loads(r[1] or "[]"):
                    owners.setdefault(pk, set()).add(r[0])
        if not owners:
            return []
        keys = list(owners)
        out = []
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            rows = self.query(
                f"""SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY posting_key ORDER BY observed_at DESC)
                                   AS rn FROM posting_versions WHERE posting_key IN ({marks})) WHERE rn = 1""",
                chunk, table="posting_versions")
            for v in rows:
                v.pop("rn", None)
                out += [{**v, "job_id": j} for j in sorted(owners[v["posting_key"]])]
        return out

    # ------------------------------------------------------------- decisions (from the picks branch)
    def import_decisions(self, rows: list[dict[str, Any]]) -> int:
        """Append decisions not seen yet (same job_id, status and time = the same decision)."""
        have = {(r[0], r[1], r[2]) for r in self.conn.execute("SELECT job_id, status, at FROM decisions")}
        fresh = []
        now = datetime.now(timezone.utc)
        for r in rows:
            at = _iso(_dt(r.get("at")) or now)
            if (r.get("job_id"), r.get("status"), at) in have or not r.get("job_id") or not r.get("status"):
                continue
            have.add((r["job_id"], r["status"], at))
            fresh.append({**r, "at": at, "imported_at": now})
        return self.append("decisions", fresh)

    def decisions_current(self) -> dict[str, dict[str, Any]]:
        return {r["job_id"]: r for r in self.query("SELECT * FROM v_decisions_current", table="decisions")}

    # ------------------------------------------------------------- upkeep
    def maintenance(self, *, desc_days: int = 30, sightings_days: int = 45, vacuum: bool = False) -> dict[str, int]:
        """Trim descriptions and raw payloads older than `desc_days` (except on shortlisted jobs the user picked),
        drop old sightings, and VACUUM when asked (weekly)."""
        cut = _iso(datetime.now(timezone.utc) - timedelta(days=desc_days))
        keep = {pk for (keys,) in self.conn.execute(
            """SELECT member_posting_keys FROM shortlist WHERE job_id IN
                 (SELECT job_id FROM v_decisions_current WHERE status NOT IN ('skipped', 'new'))""")
                for pk in json.loads(keys or "[]")}
        with self.conn:
            self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS keep_keys (k TEXT PRIMARY KEY)")
            self.conn.execute("DELETE FROM keep_keys")
            self.conn.executemany("INSERT OR IGNORE INTO keep_keys VALUES (?)", [(k,) for k in keep])
            trimmed = self.conn.execute(
                """UPDATE posting_versions SET description = NULL, raw = NULL
                   WHERE observed_at < ? AND (description IS NOT NULL OR raw IS NOT NULL)
                     AND posting_key NOT IN (SELECT k FROM keep_keys)""", (cut,)).rowcount
            self.conn.execute(
                """UPDATE shortlist SET description = NULL WHERE night < ? AND description IS NOT NULL
                   AND job_id NOT IN (SELECT job_id FROM v_decisions_current WHERE status NOT IN ('skipped', 'new'))""",
                (cut[:10],))
            sight = self.conn.execute("DELETE FROM posting_sightings WHERE seen_at < ?",
                                      (_iso(datetime.now(timezone.utc) - timedelta(days=sightings_days)),)).rowcount
        if vacuum:
            self.conn.execute("VACUUM")
        return {"descriptions_trimmed": trimmed, "sightings_deleted": sight, "vacuumed": int(vacuum)}


_ALL_REPEATED = {f for spec in TABLES.values() for f, _t, m in spec[0] if m == "REPEATED"}


def open_state(path: Path | str | None = None, *, read_only: bool = False) -> SQLiteStore:
    from jobradar.settings import get_settings

    return SQLiteStore(path or get_settings().state_db, read_only=read_only)
