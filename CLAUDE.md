# Job Radar: notes for Claude Code

Python 3.12+ package `jobradar-eu` (import `jobradar`, CLI `jr`). Layout and contribution rules: CONTRIBUTING.md.

## Commands
- Tests (offline, ~75 s): `.venv/bin/python -m pytest -q tests`. One file: `... tests/unit/test_src_<id>.py`.
- Run the CLI against a scratch setup, never the user's real one:
  `JR_HOME=/tmp/jr-dev JR_USER_DIR=/tmp/jr-dev/user jr <command>` (copy `examples/lisbon-backend/user` there first).
- Source list page: `jr sources --markdown > docs/sources.md` after any change to `defaults/sources.yaml` or an
  adapter's docstring (`test_sources_doc_is_current` checks it).
- Personal-data check: `python scripts/check_personal_data.py` (also runs in CI).

## Where things live
- Shipped defaults: `src/jobradar/defaults/` (sources.yaml, matching.yaml, packs/, ats_registry.csv.gz).
- A user's settings: `$JR_USER_DIR` (default `~/.job-radar/user`, pushed to their private config repo).
  Their private files: `$JR_HOME` (default `~/.job-radar`: .env, local.db, cv.yaml, experience.md, PDFs).
- Storage is SQLite only: `store/sqlite.py`, tables in `store/schema.py`.

## Rules
- No personal data anywhere in the repo: use the fictional example user and companies like "Acme".
- Sources: requests only through `ctx.http` (PoliteClient); keep budgets small; no proxies, impersonation or
  challenge solving. A board that blocks GitHub gets `runner: local`.
- Auto-fill (`extension/filler.js`) must never click Submit or press Enter.
- A matcher change needs a golden case (`test_matcher_golden.py`, or the pack's `golden:` list).

## Skills
- `/probe-source <id>`: test or fix one source.
- `/explain-job <id>`: why a job was shortlisted or dropped.
