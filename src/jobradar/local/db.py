"""~/.job-radar/local.db: everything the user does in the app. Never pushed except the decision events
(status changes) that `jr picks push` appends to the config repo's `picks` branch.

Tables
  events  every decision (new → picked / skipped / applied / screening / interview / offer / rejected), with a
          `pushed` flag; the current status of a job is its latest event
  jobs    per-job data the app keeps: notes, chosen CV, prep state, and hand-added jobs (M- ids) that are not in
          state.db
  bank    the answer bank (Draft / Approved), aliases, uses
  prep    the cached prep of each job (JSON)
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUSES = ("new", "picked", "skipped", "applied", "screening", "interview", "offer", "rejected", "later")
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, status TEXT NOT NULL, at TEXT NOT NULL,
  title TEXT, company TEXT, family TEXT, source TEXT, route TEXT, dedup_keys TEXT, note TEXT,
  pushed INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_events_job ON events(job_id, id);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY, title TEXT, company TEXT, url TEXT, posting_url TEXT, location TEXT, description TEXT,
  family TEXT, manual INTEGER NOT NULL DEFAULT 0, notes TEXT, cv_variant TEXT, prep TEXT, effort_min INTEGER,
  apply_url TEXT, applied_on TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS bank (
  answer_id TEXT PRIMARY KEY, kind TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '',
  short TEXT NOT NULL DEFAULT '', aliases TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'Draft',
  uses INTEGER NOT NULL DEFAULT 0, updated_at TEXT);
CREATE TABLE IF NOT EXISTS prep (job_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# The answer bank's starting rows (all Draft until the user approves them). Contact facts start empty; the wizard
# fills them locally. EEO rows default to "prefer not to say".
NA_EEO = "I don't wish to answer"
SEED_BANK: list[tuple[str, str, str, str]] = [
    ("first_name", "fact", "First name", ""),
    ("last_name", "fact", "Last name", ""),
    ("full_name", "fact", "Full name", ""),
    ("email", "fact", "Email", ""),
    ("phone", "fact", "Phone", ""),
    ("linkedin", "fact", "LinkedIn profile", ""),
    ("github", "fact", "GitHub profile", ""),
    ("website", "fact", "Website / portfolio", ""),
    ("city", "fact", "Current location (city)", ""),
    ("country", "fact", "Country of residence", ""),
    ("current_company", "fact", "Current company", ""),
    ("current_title", "fact", "Current job title", ""),
    ("years_experience", "fact", "Years of relevant experience", ""),
    ("notice_period", "fact", "What is your notice period?", ""),
    ("salary_expectation", "fact", "What are your salary expectations?", ""),
    ("work_authorization", "fact", "Are you legally authorized to work in the country of this job?", ""),
    ("visa_sponsorship", "fact", "Will you now or in the future require visa sponsorship?", ""),
    ("relocation", "fact", "Are you willing to relocate?", ""),
    ("office_presence", "fact", "Are you able to work from the office as required for this role?", ""),
    ("education_school", "fact", "School / university", ""),
    ("education_degree", "fact", "Degree", ""),
    ("education_discipline", "fact", "Discipline / field of study", ""),
    ("education_start_year", "fact", "Education start year", ""),
    ("education_end_year", "fact", "Education end year", ""),
    ("languages", "fact", "Which languages do you speak and at what level?", ""),
    ("how_heard", "fact", "How did you hear about this job?", "Company careers page"),
    ("worked_here_before", "fact", "Have you worked for {company} before?", "No"),
    ("non_compete", "fact", "Are you subject to a non-compete agreement?", "No"),
    ("pronouns", "eeo", "Pronouns", NA_EEO),
    ("gender", "eeo", "Gender", NA_EEO),
    ("ethnicity", "eeo", "Race / ethnicity", NA_EEO),
    ("veteran", "eeo", "Veteran status", NA_EEO),
    ("disability", "eeo", "Disability status", NA_EEO),
    ("privacy_consent", "consent", "I agree to the processing of my personal data / privacy policy", "Yes"),
    ("ai_notetaker_consent", "consent", "Do you consent to an AI notetaker being used during interviews?", ""),
    ("why_company", "company", "Why do you want to work at {company}?", ""),
    ("why_role", "company", "Why are you interested in this role?", ""),
    ("cover_letter", "story", "Cover letter", ""),
    ("proud_project", "story", "Tell us about a project you are proud of", ""),
    ("stakeholders", "story", "How do you work with stakeholders?", ""),
    ("why_leaving", "story", "Why are you looking to leave your current role?", ""),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LocalDB:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._local = threading.local()
        self._lock = threading.Lock()

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            c.row_factory = sqlite3.Row
            c.executescript(SCHEMA)
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # ------------------------------------------------------------------ decisions
    def record(self, job_id: str, status: str, *, title: str = "", company: str = "", family: str | None = None,
               source: str | None = None, route: str | None = None, dedup_keys: list[str] | None = None,
               note: str | None = None, at: str | None = None) -> dict[str, Any]:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        row = {"job_id": job_id, "status": status, "at": at or now_iso(), "title": title, "company": company,
               "family": family, "source": source, "route": route, "dedup_keys": json.dumps(dedup_keys or []),
               "note": note}
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO events (job_id, status, at, title, company, family, source, route, dedup_keys, note) "
                "VALUES (:job_id, :status, :at, :title, :company, :family, :source, :route, :dedup_keys, :note)", row)
            if status == "applied":
                self.conn.execute("INSERT INTO jobs (job_id, applied_on, created_at, updated_at) VALUES (?, ?, ?, ?) "
                                  "ON CONFLICT(job_id) DO UPDATE SET applied_on = COALESCE(applied_on, excluded.applied_on), "
                                  "updated_at = excluded.updated_at", (job_id, row["at"][:10], row["at"], row["at"]))
        return row

    def statuses(self) -> dict[str, dict[str, Any]]:
        """Latest event per job."""
        rows = self.conn.execute(
            "SELECT e.* FROM events e JOIN (SELECT job_id, MAX(id) AS id FROM events GROUP BY job_id) l USING (id)"
        ).fetchall()
        return {r["job_id"]: dict(r) for r in rows}

    def status(self, job_id: str) -> str:
        r = self.conn.execute("SELECT status FROM events WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        return r[0] if r else "new"

    def history(self, job_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM events WHERE job_id = ? ORDER BY id", (job_id,))]

    def unpushed(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM events WHERE pushed = 0 ORDER BY id")]

    def mark_pushed(self, ids: list[int]) -> None:
        with self._lock, self.conn:
            self.conn.executemany("UPDATE events SET pushed = 1 WHERE id = ?", [(i,) for i in ids])

    def import_events(self, rows: list[dict[str, Any]]) -> int:
        """Decisions already on the `picks` branch (another computer): added as pushed."""
        have = {(r[0], r[1], r[2]) for r in self.conn.execute("SELECT job_id, status, at FROM events")}
        n = 0
        with self._lock, self.conn:
            for r in rows:
                key = (r.get("job_id"), r.get("status"), r.get("at"))
                if key in have or not all(key) or r.get("status") not in STATUSES:
                    continue
                have.add(key)
                self.conn.execute(
                    "INSERT INTO events (job_id, status, at, title, company, family, source, route, dedup_keys, note, "
                    "pushed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (*key, r.get("title"), r.get("company"), r.get("family"), r.get("source"), r.get("route"),
                     json.dumps(r.get("dedup_keys") or []), r.get("note")))
                n += 1
        return n

    # ------------------------------------------------------------------ per-job data
    def job(self, job_id: str) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(r) if r else {}

    def set_job(self, job_id: str, **fields: Any) -> None:
        cols = ["job_id", "updated_at", *fields]
        vals = [job_id, now_iso(), *fields.values()]
        sets = ", ".join(f"{c} = excluded.{c}" for c in cols[1:])
        with self._lock, self.conn:
            self.conn.execute(
                f"INSERT INTO jobs ({', '.join(cols)}, created_at) VALUES ({', '.join('?' for _ in cols)}, ?) "
                f"ON CONFLICT(job_id) DO UPDATE SET {sets}", (*vals, vals[1]))

    def manual_jobs(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM jobs WHERE manual = 1 ORDER BY created_at DESC")]

    def add_manual_job(self, *, title: str, company: str, url: str, location: str = "", description: str = "",
                       apply_url: str = "") -> str:
        job_id = f"M-{uuid.uuid4().hex[:6].upper()}"
        self.set_job(job_id, title=title, company=company, url=url, posting_url=url, location=location,
                     description=description, manual=1, apply_url=apply_url or url)
        return job_id

    # ------------------------------------------------------------------ prep cache
    def prep(self, job_id: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT data FROM prep WHERE job_id = ?", (job_id,)).fetchone()
        return json.loads(r[0]) if r else None

    def save_prep(self, prep: dict[str, Any]) -> None:
        with self._lock, self.conn:
            self.conn.execute("INSERT INTO prep (job_id, data, updated_at) VALUES (?, ?, ?) ON CONFLICT(job_id) DO "
                              "UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                              (prep["job_id"], json.dumps(prep, ensure_ascii=False, default=str), now_iso()))

    # ------------------------------------------------------------------ answer bank
    def seed_bank(self) -> int:
        """Add the starting rows that are missing (never overwrites)."""
        have = {r[0] for r in self.conn.execute("SELECT answer_id FROM bank")}
        rows = [(i, k, q, a, now_iso()) for i, k, q, a in SEED_BANK if i not in have]
        with self._lock, self.conn:
            self.conn.executemany(
                "INSERT INTO bank (answer_id, kind, question, answer, updated_at) VALUES (?, ?, ?, ?, ?)", rows)
        return len(rows)

    def bank_rows(self) -> list[dict[str, Any]]:
        out = []
        for r in self.conn.execute("SELECT * FROM bank ORDER BY kind, answer_id"):
            d = dict(r)
            d["aliases"] = json.loads(d["aliases"] or "[]")
            out.append(d)
        return out

    def bank(self):
        from jobradar.apply.bank import Answer, Bank

        return Bank([Answer(answer_id=r["answer_id"], kind=r["kind"], question=r["question"], answer=r["answer"],
                            short=r["short"], aliases=r["aliases"], status=r["status"], uses=r["uses"],
                            page_id=r["answer_id"]) for r in self.bank_rows()])

    def upsert_answer(self, answer_id: str, *, question: str | None = None, kind: str | None = None,
                      answer: str | None = None, short: str | None = None, status: str | None = None,
                      aliases: list[str] | None = None, page_id: str | None = None) -> str:
        cur = self.conn.execute("SELECT * FROM bank WHERE answer_id = ?", (answer_id,)).fetchone()
        d = dict(cur) if cur else {"answer_id": answer_id, "kind": kind or "fact", "question": question or answer_id,
                                   "answer": "", "short": "", "aliases": "[]", "status": "Draft", "uses": 0}
        for k, v in (("question", question), ("kind", kind), ("answer", answer), ("short", short), ("status", status)):
            if v is not None:
                d[k] = v
        if aliases is not None:
            d["aliases"] = json.dumps(list(dict.fromkeys(aliases)), ensure_ascii=False)
        d["updated_at"] = now_iso()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO bank (answer_id, kind, question, answer, short, aliases, status, uses, updated_at) "
                "VALUES (:answer_id, :kind, :question, :answer, :short, :aliases, :status, :uses, :updated_at) "
                "ON CONFLICT(answer_id) DO UPDATE SET kind = excluded.kind, question = excluded.question, "
                "answer = excluded.answer, short = excluded.short, aliases = excluded.aliases, "
                "status = excluded.status, updated_at = excluded.updated_at", d)
        return answer_id

    def add_alias(self, answer_id: str, alias: str) -> bool:
        cur = self.conn.execute("SELECT aliases FROM bank WHERE answer_id = ?", (answer_id,)).fetchone()
        if not cur:
            return False
        aliases = json.loads(cur[0] or "[]")
        if alias in aliases:
            return False
        self.upsert_answer(answer_id, aliases=[*aliases, alias])
        return True

    def count_use(self, answer_ids: list[str]) -> None:
        with self._lock, self.conn:
            self.conn.executemany("UPDATE bank SET uses = uses + 1 WHERE answer_id = ?", [(a,) for a in answer_ids])

    def delete_answer(self, answer_id: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM bank WHERE answer_id = ?", (answer_id,))

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                              "value = excluded.value", (key, value))


def open_local(path: Path | str | None = None) -> LocalDB:
    from jobradar.settings import get_settings

    return LocalDB(path or get_settings().local_db)
