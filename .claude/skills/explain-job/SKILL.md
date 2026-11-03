---
name: explain-job
description: Explain why a Job Radar posting was shortlisted (section, family, tags, fit) or dropped, from a JR-XXXXXX job id, a posting_key, a URL or a company + title. Use when the user asks why a job is or isn't in their morning list, or reports a false positive or negative.
---

# /explain-job <JR-XXXXXX | posting_key | url | "company — title">

Read-only. Commands read `~/.job-radar/state.db` (`jr state pull` downloads the newest one from the config repo;
`--db` points elsewhere).

1. Resolve the id to a posting_key:
   - `JR-XXXXXX` → `jr sql "SELECT representative_posting_key, member_posting_keys FROM shortlist WHERE job_id = 'JR-XXXXXX' ORDER BY night DESC LIMIT 1"`.
   - URL or company/title → `jr sql "SELECT posting_key, source, title, company, url FROM v_postings_current WHERE url = '<url>' OR (LOWER(company) LIKE '%<company>%' AND LOWER(title) LIKE '%<title>%') LIMIT 10"`.
   - Nothing found → no source fetched it: say which sources should have (`jr sources`), and suggest `/probe-source`.
2. `jr explain <posting_key>` → the latest match decision with its `explain` JSON (family rule hit, exclusions,
   seniority, location and work-mode evidence, language, freshness, tags, `rule_version`).
3. For the job's history (first seen, sources, nights shortlisted, the user's decision):
   `jr trace "<company>"`.
4. Answer in plain words: shortlisted into which section and why, or dropped by which rule (quote the rule and the
   text that triggered it). If the decision looks wrong, propose the smallest fix:
   - only this user's taste → `user/roles.yaml` (`never`, `never_unless`, `keep_titles`, seniority range) or
     `user/matching.yaml` in their config folder;
   - a bug in the shipped rules → `src/jobradar/defaults/matching.yaml` or the pack in `defaults/packs/`, plus a
     golden row in `tests/unit/test_matcher_golden.py` or `tests/unit/test_packs_golden.py`.

   A changed config gets a new `rule_version`, so the next `jr daily` re-matches recent postings;
   `jr rematch --days 30` does it now.
