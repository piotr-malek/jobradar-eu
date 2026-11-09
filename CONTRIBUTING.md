# Contributing

Fixes for broken sources, new job boards and new role packs are the most useful contributions. Please open an
issue first for anything bigger, such as a new module or a change to the setup wizard.

## Development setup

```sh
git clone https://github.com/piotr-malek/jobradar-eu && cd jobradar-eu
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m playwright install chromium     # for the auto-fill and PDF tests
.venv/bin/python -m pytest -q tests
```

The tests run offline: every source test replays recorded HTTP responses from `tests/fixtures/`.

To try your changes on your own data, point the commands at a scratch folder so nothing touches your real setup:

```sh
export JR_HOME=/tmp/jr-dev JR_USER_DIR=/tmp/jr-dev/user
cp -r examples/lisbon-backend/user /tmp/jr-dev/user
jr sources          # which sources run for this profile
jr probe arbeitnow  # fetch and match one source without storing anything
```

## Layout

| Path | What |
|---|---|
| `src/jobradar/sources/` | Source adapters (`boards/`, `generic/` sitemap/RSS/Algolia readers, `ats/` company boards) |
| `src/jobradar/defaults/sources.yaml` | The source list: cadence, budgets, parameters, which countries and packs switch each on |
| `src/jobradar/defaults/packs/` | Role packs: title rules, search words, stack words, board categories |
| `src/jobradar/defaults/matching.yaml` | Matching rules shared by every pack (location, language, freshness, agencies) |
| `src/jobradar/match/`, `normalize/`, `dedup/` | Matching, text and location normalising, duplicate clustering |
| `src/jobradar/pipeline/` | The overnight jobs: `daily`, `ats`, `weekly`, and the shortlist |
| `src/jobradar/app/`, `wizard.py` | The local app and the setup wizard (Flask) |
| `src/jobradar/apply/`, `extension/` | Full mode: form prep, answers, PDFs, the Chrome auto-fill extension |
| `src/jobradar/defaults/config_repo/` | The files the wizard puts in every user's config repository: the workflow and its schedule |
| `examples/lisbon-backend/` | A fictional user, used by the tests and as a starting point |

## Adding a job board

1. **Check that it's allowed.** Read the board's robots.txt and terms. Prefer a public API, RSS feed or sitemap
   over HTML pages. Don't add a board that needs a login, a proxy, browser impersonation or CAPTCHA solving.
2. **Write the adapter** in `src/jobradar/sources/boards/<name>.py`, or reuse a generic adapter: `sitemap_jsonld`
   (sitemap → schema.org `JobPosting`), `rss_feed` or `algolia_board`. An adapter is a function registered with
   `@adapter("<name>")` that takes a `FetchContext` and yields `Posting` objects:
   - Make every request through `ctx.http` (a `PoliteClient`): it spaces requests, enforces the source's budget and
     robots.txt, and records fixtures.
   - Read settings from `ctx.params`. Search words come from the user's role packs: use the `$terms` placeholder in
     sources.yaml, or `default_terms()` from `sources/base.py` as a fallback.
   - Before fetching a detail page, ask `ctx.wants_detail(title)` (title pre-filter) and skip jobs that are already
     stored with `ctx.is_known(...)` / `ctx.mark_seen(...)`.
   - Fill what the board gives in structured form (`countries`, `cities`, `workplace`, `posted_at`, salary) and leave
     the rest empty; the normalisers infer it from text. `description` is plain text.
   - The first sentence of the adapter's docstring appears in `docs/sources.md`, so make it say what the board is
     and how it's read.
3. **Add an entry** to `src/jobradar/defaults/sources.yaml` with `id`, `adapter`, `label`, `countries`, `every_days`,
   a `politeness` budget (`max_requests`, `rps`, `robots`) and `notes`. Gate it with `packs:` if it only lists one
   kind of role, and set `runner: local` if it blocks GitHub's IP addresses.
4. **Probe and record:** `jr probe <id>` prints what a run would fetch and match. `jr probe <id> --record` saves the
   responses to `tests/fixtures/<id>/`. Keep the fixture small (a few pages) and check it holds no personal data.
5. **Test:** add `tests/unit/test_src_<id>.py` that replays the fixture (see `test_src_themuse.py`).
6. **Regenerate the list:** `jr sources --markdown > docs/sources.md`. A test fails if you forget.

To fix a broken source, run `jr probe <id>` first. The `/probe-source` skill in `.claude/skills/` walks through the
same steps.

## Adding or changing a role pack

A pack is one YAML file in `src/jobradar/defaults/packs/`:

- `families`: title rules (regular expressions over the normalised, accent-free title), with `tier`, `priority` and
  `label`. `manager_ok` lets a family keep manager titles.
- `family_exclude` / `family_exclude_ok`: words that drop a title, and the exceptions.
- `allow_disciplines`: entries of `title.exclude.discipline_hard` in `matching.yaml` (roles skipped as "not IT",
  e.g. customer success) that this pack takes back while it is selected. See `sales-cs.yaml`.
- `search_terms`: words the boards are searched with, per language; English first.
- `stack`: technology words and weights used to rank jobs; `prefer`: URL slug patterns tried first.
- `boards`: category ids on boards that have them (`$board:<board>.<key>` in sources.yaml).
- `golden.keep` / `golden.drop`: real titles the pack must keep or drop. `tests/unit/test_packs_golden.py` checks
  every one, so add a title to `golden` with each rule change.

Rules shared by all packs, such as location and language, belong in `defaults/matching.yaml`, with a case in
`tests/unit/test_matcher_golden.py`.

## Rules for every change

- **No personal data.** No names, e-mail addresses, phone numbers, CVs or private configuration in code, tests or
  fixtures. Use the fictional example user and companies such as "Acme" or "Northwind Retail". CI runs a check for
  this (`scripts/check_personal_data.py`).
- **Be polite to job boards.** Keep request budgets small: thousands of people may run the same source every night.
- **No automatic submits.** Auto-fill must never press Submit or Enter on an application form.
- Tests must pass offline: `python -m pytest -q tests`.
