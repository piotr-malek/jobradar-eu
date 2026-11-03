"""Dedup, HTTP client politeness, SQLite store + shortlist end-to-end, ATS parsing."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from jobradar.dedup.cluster import ats_key, cluster_rows, fz_key, parse_ats, short_job_id
from jobradar.http.client import Blocked, BudgetExceeded, PoliteClient
from jobradar.match.engine import MatchEngine
from jobradar.models import Posting
from jobradar.normalize.lang import detect_language, language_requirements
from jobradar.normalize.location import extract_places
from jobradar.normalize.names import company_key, norm_company, norm_title, title_key
from jobradar.sources.ats.vendors import fetch_ashby, fetch_greenhouse, fetch_lever, fetch_workday
from jobradar.sources.base import SourceSpec, parse_date
from jobradar.store.sqlite import SQLiteStore

NOW = datetime.now(timezone.utc)


# ------------------------------------------------------------------ normalize
def test_company_and_title_norm():
    assert norm_company("Allegro sp. z o.o.") == "allegro"
    assert norm_company("Acme GmbH") == norm_company("ACME")
    assert norm_title("Senior Data Engineer (m/w/d) - Berlin") == "senior data engineer"
    assert norm_title("Sr. Data Engineer (Remote)") == "senior data engineer"
    assert norm_title("Data Engineer") != norm_title("Senior Data Engineer")  # seniority kept
    # work mode glued on without brackets (seen on JustJoin: DCV Technologies)
    assert norm_title("Senior Azure Data Engineer-hybrid") == norm_title("Senior Azure Data Engineer | Remote")
    assert norm_title("Lead Data Platform Engineer /GCP/Remote") == "lead data platform engineer"
    assert norm_title("BigQuery / Analytics Engineer") == "bigquery analytics engineer"


def test_dedup_company_and_title_keys():
    assert company_key("Sii Sp. z o.o.") == company_key("Sii Polska") == company_key("Sii Poland") == "sii"
    assert company_key("Logicalis Spain") == "logicalis" and company_key("EPAM Systems") == "epam systems"
    assert company_key("Constructor.io") == company_key("Constructor") == "constructor"
    same = [("Data Governance Engineer (GCP)", "Data Engineer GCP (Governance)"),            # word order
            ("Revenue Operations Manager (EMEA)", "Revenue Operations Manager - EMEA"),
            ("Software Engineer, AI Data - College Park, MD, USA", "Software Engineer, AI Data - Malaga, Spain"),
            ("Senior Data Engineer with Databricks (Warsaw)", "Senior Data Engineer with Databricks"),
            ("Data Engineer | Python + SQL + ETL | 100% Remoto", "Data Engineer (Python, SQL, ETL)"),
            ("Senior Data Engineer with GCP (f/m/x)", "Senior Data Engineer with GCP")]
    for a, b in same:
        assert title_key(a) == title_key(b), (a, b)
    different = [("Data Engineer", "Senior Data Engineer"), ("Analytics Engineer I", "Analytics Engineer"),
                 ("Enterprise Data Architect", "Data Architect"),     # Enterprise, AL is a city; not in a title
                 ("Delta Lake Engineer", "Lake Engineer"), ("Data Engineer (AWS)", "Data Engineer")]
    for a, b in different:
        assert title_key(a) != title_key(b), (a, b)
    for t in ("Data Engineer", "Senior Data Engineer (m/w/d) - Berlin", "Lead Data Platform Engineer /GCP/Remote"):
        assert title_key(t) == " ".join(sorted(norm_title(t).split()))     # equal norm_titles stay equal


def test_places():
    c, r, _ = extract_places("Wrocław, Poland")
    assert "PL" in c
    c, r, _ = extract_places("Remote - Europe")
    assert "europe" in r
    c, _, _ = extract_places("Wilmington, DE")
    assert c == {"US"}
    c, _, _ = extract_places("Berlin, DE")
    assert "DE" in c


def test_language():
    assert detect_language("We are looking for you to join our team and work with the data stack and have experience with SQL.")[0] == "en"
    assert language_requirements("Fluent German (C1) is required.")["de"] == "required"
    assert language_requirements("German is a plus.")["de"] == "plus"


def test_parse_date():
    assert parse_date("2026-10-01T10:00:00Z").year == 2026
    assert parse_date(1760000000000).year >= 2025
    assert (NOW - parse_date("Posted 3 days ago")).days in (2, 3)


# ---------------------------------------------------------------------- dedup
def test_parse_ats_urls():
    assert parse_ats("https://boards.greenhouse.io/acme/jobs/123") == ("greenhouse", "acme", "123")
    assert parse_ats("https://jobs.eu.lever.co/olx/0b6d6c6e-1111-2222-3333-444455556666")[0] == "lever"
    assert ats_key("https://jobs.ashbyhq.com/acme/0b6d6c6e-1111-2222-3333-444455556666").startswith("ats:ashby:acme:")


def test_clusters_merge_same_job_across_sources():
    rows = [
        {"posting_key": "a", "source": "justjoin", "company": "Acme sp. z o.o.", "title": "Data Engineer (k/m)", "url": "u1", "priority": 50},
        {"posting_key": "b", "source": "ats_greenhouse", "company": "ACME", "title": "Data Engineer", "url": "https://boards.greenhouse.io/acme/jobs/9", "priority": 40},
        {"posting_key": "c", "source": "adzuna", "company": "Acme", "title": "Senior Data Engineer", "url": "u3", "priority": 30},
    ]
    clusters = cluster_rows(rows, {"ats_greenhouse": 10, "justjoin": 40, "adzuna": 60})
    assert len(clusters) == 2  # senior is a different role
    merged = next(c for c in clusters if len(c.members) == 2)
    assert merged.representative["source"] == "ats_greenhouse"  # direct ATS link preferred
    assert short_job_id(merged.cluster_id).startswith("JR-") and len(short_job_id("x")) == 9


def test_same_company_and_title_is_one_job_also_on_one_board_and_for_agencies():
    # Sii lists a copy per city, as "Sii Sp. z o.o." on the pracuj alerts and "Sii Poland" on LinkedIn
    rows = [{"posting_key": f"a{i}", "source": "email_alerts", "company": "Sii Sp. z o.o.",
             "title": "Senior Data Engineer with GCP (f/m/x)", "url": f"u{i}", "flags": ["agency"]} for i in range(3)]
    rows.append({"posting_key": "b", "source": "linkedin_apify", "company": "Sii Poland",
                 "title": "Senior Data Engineer with GCP", "url": "v"})
    # Jobgether reposts the employer's own job (agency-flagged source) → same job as the LinkedIn copy
    rows += [{"posting_key": "c", "source": "linkedin_apify", "company": "OLX", "title": "Data Engineer", "url": "w"},
             {"posting_key": "d", "source": "jobgether", "company": "OLX Group", "title": "Data Engineer", "url": "x",
              "flags": ["agency"]}]
    clusters = cluster_rows(rows)
    assert sorted(len(c.members) for c in clusters) == [2, 4]


def test_sent_keys_include_company_title_key_under_todays_rules(tmp_path):
    store = SQLiteStore(tmp_path / "state.db")
    store.append("shortlist", [{"night_id": "d", "night": date.today().isoformat(), "job_id": "JR-1",
                                   "company": "Sii Polska", "title": "Senior Data Engineer with GCP",
                                   "dedup_keys": ["pk:x", "fz:sii polska|senior data engineer with gcp"]}])
    keys = store.sent_dedup_keys()
    assert "fz:sii polska|senior data engineer with gcp" in keys          # as stored
    assert f"fz:{fz_key('Sii Sp. z o.o.', 'Senior Data Engineer with GCP (f/m/x)')}" in keys


# ----------------------------------------------------------------------- http
def _client(handler, **kw):
    return PoliteClient("t", transport=httpx.MockTransport(handler), sleep=False, **kw)


def test_blocked_on_403_and_challenge_without_retry():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(403)

    with _client(handler) as c, pytest.raises(Blocked):
        c.get("https://x.test/a")
    assert len(calls) == 1
    page = b"<html><title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x'></script></html>"
    with _client(lambda r: httpx.Response(200, content=page, headers={"content-type": "text/html"})) as c, pytest.raises(Blocked):
        c.get("https://x.test/b")


def test_retry_on_5xx_then_success_and_budget():
    seq = iter([500, 502, 200])
    with _client(lambda r: httpx.Response(next(seq), json={"ok": 1})) as c:
        assert c.get_json("https://x.test/a") == {"ok": 1}
        assert c.stats.retries == 2
    with _client(lambda r: httpx.Response(200, json={}), max_requests=2) as c, pytest.raises(BudgetExceeded):
        for _ in range(3):
            c.get("https://x.test/a")


# --------------------------------------------------------------- ATS parsing
def test_ats_vendor_parsers():
    def handler(req):
        url = str(req.url)
        if "greenhouse" in url:
            return httpx.Response(200, json={"jobs": [{"id": 1, "title": "Analytics Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                                                     "location": {"name": "Wrocław, Poland"}, "content": "&lt;p&gt;dbt&lt;/p&gt;", "updated_at": "2026-10-20T00:00:00Z"}]})
        if "api.lever.co" in url:
            return httpx.Response(404, json={"ok": False})
        if "api.eu.lever.co" in url:
            return httpx.Response(200, json=[{"id": "x", "text": "Data Engineer", "hostedUrl": "h", "workplaceType": "remote",
                                              "categories": {"location": "Remote - EU"}, "createdAt": 1760000000000}])
        if "ashbyhq" in url:
            return httpx.Response(200, json={"jobs": [{"id": "a", "title": "Data Engineer", "isListed": True, "workplaceType": "Hybrid",
                                                       "location": "Warsaw", "jobUrl": "j", "publishedAt": "2026-10-20T00:00:00Z"},
                                                      {"id": "b", "title": "Hidden", "isListed": False}]})
        if "myworkdayjobs" in url and url.endswith("/jobs"):
            return httpx.Response(200, json={"total": 1, "jobPostings": [{"title": "Data Engineer", "externalPath": "/job/Wroclaw/Data-Engineer_R1", "locationsText": "Wroclaw, Poland", "postedOn": "Posted Today"}]})
        if "myworkdayjobs" in url:
            return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "<p>dbt</p>", "location": "Wroclaw", "country": {"descriptor": "Poland"}}})
        return httpx.Response(404)

    with _client(handler) as http:
        code, posts = fetch_greenhouse(http, "acme", {}, lambda t: True)
        assert code == 200 and posts[0].description == "dbt" and posts[0].ats_job_id == "1"
        extra: dict = {}
        code, posts = fetch_lever(http, "olx", extra, lambda t: True)
        assert code == 200 and posts[0].workplace == "remote" and extra["region"] == "eu"
        code, posts = fetch_ashby(http, "acme", {}, lambda t: True)
        assert [p.title for p in posts] == ["Data Engineer"] and posts[0].workplace == "hybrid"
        code, posts = fetch_workday(http, "aptiv", {"tenant": "aptiv", "shard": "wd5", "site": "x", "search": ["data"]}, lambda t: True)
        assert posts[0].ats_job_id == "R1" and "Poland" in posts[0].location_raw and posts[0].description == "dbt"


# ------------------------------------------------- local store + digest e2e
def _posting(i: int, title: str = "Data Engineer", loc: str = "Wrocław, Poland") -> Posting:
    return Posting(source="arbeitnow", source_job_id=str(i), title=f"{title} {i}" if i else title, company=f"Company {i}",
                   url=f"https://example.com/{i}", location_raw=loc,
                   description="We are looking for you to join our team and work with dbt and BigQuery and SQL pipelines. " * 3,
                   posted_at=NOW - timedelta(days=1))


def _load(store, posts, engine):
    from jobradar.pipeline.run import process_postings

    v, s, m, stats = process_postings(posts, spec=SourceSpec(id="arbeitnow", adapter="arbeitnow"), engine=engine,
                                      known={}, run_id="r")
    for t, rows in (("posting_versions", v), ("posting_sightings", s), ("matches", m)):
        store.append(t, rows)
    return stats


def test_store_and_shortlist_end_to_end(tmp_path):
    from jobradar.pipeline.run import process_postings
    from jobradar.pipeline.shortlist import build_shortlist

    store = SQLiteStore(tmp_path / "state.db")
    engine = MatchEngine.load()
    posts = [_posting(i) for i in range(1, 6)]
    assert _load(store, posts, engine)["matched"] == 5
    # second run: nothing new
    v2, _, _, stats2 = process_postings(posts, spec=SourceSpec(id="arbeitnow", adapter="arbeitnow"), engine=engine,
                                        known=store.known_versions("arbeitnow"), run_id="r2")
    assert stats2["new_versions"] == 0 and not v2

    out = build_shortlist(store, engine=engine)
    assert out["status"] == "done" and out["shortlisted"] == 5
    rows = store.read("shortlist")
    assert len(rows) == 5 and all(r["job_id"].startswith("JR-") for r in rows)
    assert rows[0]["why"] and json.loads(rows[0]["why"])["stack"]
    assert build_shortlist(store, engine=engine)["status"] == "already_done"          # one shortlist per night
    out = build_shortlist(store, engine=engine, force=True)
    assert out["shortlisted"] == 0                                                   # shown jobs never come back


def test_skipped_jobs_never_come_back(tmp_path):
    from jobradar.pipeline.shortlist import build_shortlist

    store = SQLiteStore(tmp_path / "state.db")
    engine = MatchEngine.load()
    _load(store, [_posting(1, "Analytics Engineer", "Remote - Europe")], engine)
    preview = build_shortlist(store, engine=engine, dry_run=True)["preview"]
    job_id = preview[0]["job_id"]
    store.import_decisions([{"job_id": job_id, "status": "skipped", "at": NOW.isoformat(),
                             "title": "Analytics Engineer", "company": "Acme 1"}])
    assert store.import_decisions([{"job_id": job_id, "status": "skipped", "at": NOW.isoformat()}]) == 0   # idempotent
    assert build_shortlist(store, engine=engine)["shortlisted"] == 0


def test_shortlist_lookback_days(tmp_path, monkeypatch):
    from jobradar.pipeline.shortlist import build_shortlist

    store = SQLiteStore(tmp_path / "state.db")
    engine = MatchEngine.load()
    _load(store, [_posting(1, "Analytics Engineer", "Remote - Europe")], engine)
    later = datetime.now(timezone.utc) + timedelta(days=6)               # first seen 6 days before the run
    assert build_shortlist(store, engine=engine, now=later, dry_run=True)["would_shortlist"] == 0
    monkeypatch.setenv("JR_LOOKBACK_DAYS", "30")
    assert build_shortlist(store, engine=engine, now=later, dry_run=True)["would_shortlist"] == 1


def test_maintenance_trims_old_descriptions(tmp_path):
    store = SQLiteStore(tmp_path / "state.db")
    engine = MatchEngine.load()
    _load(store, [_posting(1, "Analytics Engineer", "Remote - Europe")], engine)
    store.execute("UPDATE posting_versions SET observed_at = '2020-01-01T00:00:00+00:00'")
    res = store.maintenance(vacuum=True)
    assert res["descriptions_trimmed"] == 1 and res["vacuumed"] == 1
    assert store.read("posting_versions")[0]["description"] is None


def test_shape_row_serializes():
    from jobradar.store.base import shape_row

    row = shape_row("matches", {"posting_key": "p", "rule_version": "v", "matched_at": NOW, "explain": {"a": 1},
                                "flags": ["x"], "countries": None})
    assert json.loads(row["explain"]) == {"a": 1} and row["countries"] == [] and isinstance(row["matched_at"], str)



def test_fix_mojibake_repairs_double_encoded_text_only():
    from jobradar.normalize.text import fix_mojibake

    assert fix_mojibake("Sothebyâ\u0080\u0099s") == "Sotheby’s"          # as RemoteOK sent it
    assert fix_mojibake("Sothebyâ€™s") == "Sotheby’s"          # the Windows-1252 variant
    assert fix_mojibake("CafÃ© Lisboa") == "Café Lisboa"
    for fine in ("Wrocław", "São Paulo", "München – Senior", "Sotheby’s", ""):
        assert fix_mojibake(fine) == fine
