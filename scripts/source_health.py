#!/usr/bin/env python3
"""Weekly source check: a dry run of every source that runs on GitHub, written as a Markdown report.

    JR_USER_DIR=examples/lisbon-backend/user python scripts/source_health.py report.md --minutes 40

Nothing is stored. Each source keeps its usual request budget. Sources with missing keys are listed as skipped.
Exit code 0 always: the report (and the issue the workflow keeps updated) is the result.
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone
from pathlib import Path

BROKEN = {"blocked", "failed", "empty"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--minutes", type=float, default=40)
    ap.add_argument("sources", nargs="*", help="only these sources")
    a = ap.parse_args()

    from jobradar.pipeline.run import run_sources
    from jobradar.sources.registry import load_specs

    specs = load_specs(all_sources=True)
    ids = a.sources or [sid for sid, s in specs.items() if s.enabled and s.runner in ("github", "any")]
    results = run_sources(ids, None, dry_run=True, trigger="health", deadline=time.monotonic() + a.minutes * 60)

    def broken(r) -> bool:
        return r.status in BROKEN or (r.status in ("success", "partial") and not r.fetched)

    rows = sorted(results, key=lambda r: (not broken(r), r.status == "skipped_no_key", r.source_id))
    n_broken = sum(broken(r) for r in results)
    where = "GitHub Actions" if os.environ.get("GITHUB_ACTIONS") else "a local machine"
    lines = [f"Source check of {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC from {where}: "
             f"**{n_broken} of {len(results)} sources look broken.**", "",
             "Broken first. `jr probe <id>` shows the detail; see the `/probe-source` steps in CONTRIBUTING.md.", "",
             "| Source | Status | Requests | Fetched | Matched | Detail |", "|---|---|---|---|---|---|"]
    for r in rows:
        detail = (r.error or "; ".join(r.notes[:2]) or "").replace("|", "\\|").replace("\n", " ")[:200]
        flag = "❌ " if broken(r) else ""
        lines.append(f"| {flag}`{r.source_id}` | {r.status} | {r.requests} | {r.fetched} | {r.matched} | {detail} |")
    a.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{n_broken} broken of {len(results)}; report in {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
