"""Table definitions for state.db (SQLite). Most tables are append-only; current state comes from views/queries."""

from __future__ import annotations

S, I, F, B, T, D = "STRING", "INT64", "FLOAT64", "BOOL", "TIMESTAMP", "DATE"
R = "REPEATED"

# name -> (fields[(name, type, mode)], partition_field, cluster_fields, partition_expiration_days)
TABLES: dict[str, tuple[list[tuple[str, str, str]], str | None, list[str], int | None]] = {
    "source_runs": (
        [
            ("run_id", S, "REQUIRED"), ("source_id", S, "REQUIRED"), ("kind", S, "NULLABLE"),
            ("runner", S, "NULLABLE"), ("trigger", S, "NULLABLE"), ("started_at", T, "REQUIRED"),
            ("finished_at", T, "NULLABLE"), ("status", S, "NULLABLE"), ("requests", I, "NULLABLE"),
            ("retries", I, "NULLABLE"), ("status_counts", S, "NULLABLE"), ("bytes", I, "NULLABLE"),
            ("records_fetched", I, "NULLABLE"), ("new_versions", I, "NULLABLE"), ("sightings", I, "NULLABLE"),
            ("matched", I, "NULLABLE"), ("dropped", I, "NULLABLE"), ("error", S, "NULLABLE"),
            ("rule_version", S, "NULLABLE"), ("code_version", S, "NULLABLE"), ("notes", S, "NULLABLE"),
        ],
        "started_at", ["source_id"], None,
    ),
    "posting_versions": (
        [
            ("posting_key", S, "REQUIRED"), ("source", S, "REQUIRED"), ("source_job_id", S, "NULLABLE"),
            ("version_hash", S, "REQUIRED"), ("run_id", S, "NULLABLE"), ("observed_at", T, "REQUIRED"),
            ("title", S, "NULLABLE"), ("company", S, "NULLABLE"), ("company_norm", S, "NULLABLE"),
            ("url", S, "NULLABLE"), ("apply_url", S, "NULLABLE"), ("location_raw", S, "NULLABLE"),
            ("countries", S, R), ("cities", S, R), ("workplace", S, "NULLABLE"), ("remote_scope", S, "NULLABLE"),
            ("description", S, "NULLABLE"), ("posted_at", T, "NULLABLE"), ("employment_types", S, R),
            ("salary_min", F, "NULLABLE"), ("salary_max", F, "NULLABLE"), ("salary_currency", S, "NULLABLE"),
            ("salary_period", S, "NULLABLE"), ("tags", S, R), ("ats_type", S, "NULLABLE"),
            ("ats_slug", S, "NULLABLE"), ("ats_job_id", S, "NULLABLE"), ("ats_key", S, "NULLABLE"),
            ("fz_key", S, "NULLABLE"), ("raw", S, "NULLABLE"),
        ],
        "observed_at", ["source", "posting_key"], None,
    ),
    "posting_sightings": (
        [("posting_key", S, "REQUIRED"), ("source", S, "REQUIRED"), ("run_id", S, "NULLABLE"),
         ("seen_at", T, "REQUIRED"), ("version_hash", S, "NULLABLE")],
        "seen_at", ["source", "posting_key"], 400,
    ),
    "matches": (
        [
            ("posting_key", S, "REQUIRED"), ("version_hash", S, "NULLABLE"), ("rule_version", S, "REQUIRED"),
            ("matched_at", T, "REQUIRED"), ("tier", S, "NULLABLE"), ("family", S, "NULLABLE"),
            ("section", S, "NULLABLE"), ("decision", S, "NULLABLE"), ("drop_reason", S, "NULLABLE"),
            ("work_mode", S, "NULLABLE"), ("countries", S, R), ("flags", S, R), ("stack_hits", S, R),
            ("seniority", S, "NULLABLE"), ("contract", S, R), ("posting_lang", S, "NULLABLE"),
            ("priority", F, "NULLABLE"), ("explain", S, "NULLABLE"), ("source", S, "NULLABLE"),
            ("title", S, "NULLABLE"), ("company", S, "NULLABLE"),
        ],
        "matched_at", ["rule_version", "posting_key"], None,
    ),
    "cluster_map": (
        [("posting_key", S, "REQUIRED"), ("cluster_id", S, "REQUIRED"), ("refined_at", T, "REQUIRED")],
        None, ["cluster_id"], None,
    ),
    "llm_usage": (            # LLM requests per provider and run, so the daily budget holds across the day's runs
        [("provider", S, "REQUIRED"), ("day", D, "REQUIRED"), ("requests", I, "REQUIRED"), ("at", T, "REQUIRED")],
        "day", ["provider"], None,
    ),
    "filter_suggestions": (   # "skip titles with ..." proposals from Not relevant jobs, ticked by the user in Notion
        [("suggestion_id", S, "REQUIRED"), ("kind", S, "REQUIRED"), ("value", S, "NULLABLE"), ("why", S, "NULLABLE"),
         ("examples", S, R), ("status", S, "NULLABLE"), ("block_id", S, "NULLABLE"), ("page_id", S, "NULLABLE"),
         ("created_at", T, "REQUIRED"), ("closed_at", T, "NULLABLE")],
        "created_at", [], None,
    ),
    "run_minutes": (          # each overnight job's length, for the Actions minutes governor (pipeline/minutes.py)
        [("kind", S, "REQUIRED"), ("at", T, "REQUIRED"), ("minutes", F, "REQUIRED")],
        "at", [], None,
    ),
    "nights": (               # one row per overnight shortlist (the "one shortlist per night" guard)
        [
            ("night_id", S, "REQUIRED"), ("night", D, "REQUIRED"), ("kind", S, "NULLABLE"),
            ("status", S, "NULLABLE"), ("finished_at", T, "NULLABLE"), ("n_candidates", I, "NULLABLE"),
            ("n_items", I, "NULLABLE"), ("n_screened", I, "NULLABLE"), ("rule_version", S, "NULLABLE"),
            ("stats", S, "NULLABLE"),
        ],
        "night", [], None,
    ),
    "shortlist": (            # jobs put in front of the user, one row per job and night (the app reads these)
        [
            ("night_id", S, "REQUIRED"), ("night", D, "REQUIRED"), ("job_id", S, "REQUIRED"),
            ("cluster_id", S, "NULLABLE"), ("member_posting_keys", S, R), ("dedup_keys", S, R),
            ("representative_posting_key", S, "NULLABLE"), ("section", S, "NULLABLE"), ("family", S, "NULLABLE"),
            ("family_label", S, "NULLABLE"), ("rank", I, "NULLABLE"), ("in_body", B, "NULLABLE"),
            ("title", S, "NULLABLE"), ("company", S, "NULLABLE"), ("location_display", S, "NULLABLE"),
            ("work_mode", S, "NULLABLE"), ("countries", S, R), ("url", S, "NULLABLE"), ("posting_url", S, "NULLABLE"),
            ("sources", S, R), ("priority", F, "NULLABLE"), ("flags", S, R), ("stack_hits", S, R),
            ("seniority", S, "NULLABLE"), ("salary", S, "NULLABLE"), ("posted_at", T, "NULLABLE"),
            ("description", S, "NULLABLE"), ("copies", S, "NULLABLE"), ("routes", S, "NULLABLE"),
            ("route_vendor", S, "NULLABLE"), ("route_url", S, "NULLABLE"), ("readable", B, "NULLABLE"),
            ("effort_min", I, "NULLABLE"), ("fit", I, "NULLABLE"), ("summary", S, "NULLABLE"),
            ("watch_out", S, R), ("instructions", S, R), ("work_setup", S, "NULLABLE"),
            ("screen_salary", S, "NULLABLE"), ("why", S, "NULLABLE"), ("same_company", S, "NULLABLE"),
            ("hidden", S, "NULLABLE"), ("languages_required", S, "NULLABLE"), ("years_required", S, "NULLABLE"),
            ("contract", S, "NULLABLE"), ("apply_deadline", S, "NULLABLE"), ("screen_model", S, "NULLABLE"),
            ("screened_at", T, "NULLABLE"),
            ("notion_page_id", S, "NULLABLE"), ("notion_synced_at", T, "NULLABLE"),
        ],
        "night", ["job_id"], None,
    ),
    "decisions": (            # the user's picks/skips/applications, imported from the `picks` branch
        [
            ("job_id", S, "REQUIRED"), ("status", S, "REQUIRED"), ("at", T, "REQUIRED"), ("title", S, "NULLABLE"),
            ("company", S, "NULLABLE"), ("dedup_keys", S, R), ("family", S, "NULLABLE"), ("source", S, "NULLABLE"),
            ("route", S, "NULLABLE"), ("note", S, "NULLABLE"), ("imported_at", T, "NULLABLE"),
        ],
        "at", ["job_id"], None,
    ),
    "ats_registry": (
        [
            ("ats_type", S, "REQUIRED"), ("ats_slug", S, "REQUIRED"), ("company_name", S, "NULLABLE"),
            ("extra", S, "NULLABLE"), ("origin", S, "NULLABLE"), ("discovered_at", T, "NULLABLE"),
        ],
        None, ["ats_type"], None,
    ),
    "ats_poll_results": (
        [
            ("ats_type", S, "REQUIRED"), ("ats_slug", S, "REQUIRED"), ("run_id", S, "NULLABLE"),
            ("polled_at", T, "REQUIRED"), ("http_status", I, "NULLABLE"), ("n_jobs", I, "NULLABLE"),
            ("n_eu", I, "NULLABLE"), ("n_title_ok", I, "NULLABLE"), ("n_matched", I, "NULLABLE"),
            ("error", S, "NULLABLE"),
        ],
        "polled_at", ["ats_type", "ats_slug"], None,
    ),
    "source_probes": (
        [
            ("probe_id", S, "REQUIRED"), ("source_id", S, "REQUIRED"), ("probed_at", T, "REQUIRED"),
            ("runner", S, "NULLABLE"), ("ok", B, "NULLABLE"), ("status", S, "NULLABLE"),
            ("requests", I, "NULLABLE"), ("records", I, "NULLABLE"), ("title_relevant", I, "NULLABLE"),
            ("matched", I, "NULLABLE"), ("pct_with_description", F, "NULLABLE"),
            ("median_age_days", F, "NULLABLE"), ("notes", S, "NULLABLE"),
        ],
        "probed_at", ["source_id"], None,
    ),
    "rule_versions": (
        [("rule_version", S, "REQUIRED"), ("created_at", T, "REQUIRED"), ("yaml", S, "NULLABLE")],
        None, [], None,
    ),
}
