---
name: probe-source
description: Test one Job Radar source end to end without storing anything (fetch, parse, match), find out why it returns little or nothing, and optionally record fixtures. Use when the user asks whether a source works, why a source shows as failing, or wants to add or fix a source.
---

# /probe-source <source_id>

Nothing is written to state.db unless `--save` is passed.

1. `jr sources --all`: confirm the id exists and whether it runs for this user; note `every`, `runner` and missing
   keys.
2. `jr probe <id>`: a dry run. Read the JSON summary:
   - `status`: `success` | `partial` (budget hit or blocked mid-run) | `blocked` (403/429/challenge or robots) |
     `failed` (exception) | `empty` | `skipped_no_key`.
   - `requests` against `politeness.max_requests` in `src/jobradar/defaults/sources.yaml`; `fetched` (postings
     the source listed), `title_relevant`, `matched`, `pct_with_description`, `median_age_days`, `notes` (adapter
     diagnostics), `samples`.
3. Recent nightly runs: `jr sql "SELECT * FROM v_source_health WHERE source_id = '<id>'"` (last 14 days).
4. Find the cause, in this order: missing key → robots or challenge (`blocked`) → HTTP errors in `notes` → parser
   drift (fetched 0 with HTTP 200: inspect one response with a single `curl`) → matcher drops (`jr explain
   <posting_key>` on a sample, or `jr sql "SELECT * FROM v_drop_audit WHERE source = '<id>' LIMIT 20"`).
5. If the board blocks GitHub's IPs but works from a home connection, set `runner: local` for it. Never add
   proxies, browser impersonation or challenge solving.
6. To fix a parser: change the adapter, `jr probe <id> --record` to refresh `tests/fixtures/<id>/`, update or add
   `tests/unit/test_src_<id>.py`, run `python -m pytest -q tests`. After editing `defaults/sources.yaml`, regenerate
   the source list: `jr sources --markdown > docs/sources.md`.

Report: status, numbers, the cause in one line, and the change you propose (or made).
