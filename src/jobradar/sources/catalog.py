"""The shipped source list as Markdown (`jr sources --markdown` writes docs/sources.md).

Built only from ``defaults/sources.yaml`` and the adapters' docstrings, never from a user's config, so the page
is the same for everyone and a test can check that the committed copy is current.
"""

from __future__ import annotations

import inspect
import re
from typing import Any

from jobradar.sources.registry import PAN_EU, _raw_entries, get_adapter

HEADER = """# Sources

Generated from `src/jobradar/defaults/sources.yaml` by `jr sources --markdown > docs/sources.md`; edit the YAML
(or an adapter's docstring), not this page.

Which of these run for you depends on your setup (`jr sources` lists them with on/off):

- **Covers**: countries the board lists jobs for. `EU` boards (pan-European or remote) always run. A country board
  runs when that country is in your `profile.yaml` (home or work countries).
- **Needs**: `key` sources stay off until the named environment variables exist (repository secrets on GitHub).
  `opt-in` sources run only when you switch that track on in `user/sources.yaml`. `packs` sources run only when
  one of those role packs is active.
- **Runs on**: `github` runs in your config repo's nightly workflow. `local` boards block GitHub's IP addresses,
  so they run only from your own computer (`jr run --runner local`). `any` runs in both places.
- **Every**: how many days between runs.

When the same job appears on several sources, the link kept is the source with the lowest rank: company ATS 10 ·
open job datasets 15 · national employment services 35 · national boards 40 · regional boards 45 · remote boards
and agencies 50 · aggregators 55-60.

Company ATS boards (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, Personio, Teamtailor, Workday
and others) are not listed here: Full mode polls the boards in `defaults/ats_registry.csv.gz` with `jr ats`.
"""


def _summary(adapter: str) -> str:
    """First sentence of the adapter's docstring (else its module's), one line, pipes escaped."""
    try:
        fn = get_adapter(adapter)
    except KeyError:
        return ""
    doc = inspect.getdoc(fn) or inspect.getdoc(inspect.getmodule(fn)) or ""
    para = " ".join(doc.split("\n\n")[0].split())
    m = re.match(r"(.+?[.!?])(\s|$)", para)
    return _md(m.group(1) if m else para)


def _md(text: str) -> str:
    """One table cell: reST ``code`` → Markdown `code`, pipes escaped."""
    return text.replace("``", "`").replace("|", "\\|")


def _needs(e: dict[str, Any]) -> str:
    out = []
    if e.get("secrets"):
        out.append("key: " + ", ".join(f"`{s}`" for s in e["secrets"]))
    if e.get("opt_in"):
        out.append(f"opt-in: {e['opt_in']}")
    if e.get("packs"):
        out.append("packs: " + ", ".join(e["packs"]))
    if e.get("enabled") is False:
        out.append("**off by default**")
    return "; ".join(out)


def _section(e: dict[str, Any]) -> str:
    countries = [c.upper() for c in e.get("countries") or ["EU"]]
    if e.get("track") == "purpose" or e.get("opt_in") == "purpose":
        return "Purpose-driven employers (opt-in)"
    if any(c in PAN_EU for c in countries):
        return "Pan-European and remote"
    return "Country boards"


ORDER = ["Pan-European and remote", "Country boards", "Purpose-driven employers (opt-in)"]


def markdown() -> str:
    defaults, entries = _raw_entries()
    rows: dict[str, list[tuple[str, str]]] = {k: [] for k in ORDER}
    for raw in entries:
        e = {**defaults, **raw}
        countries = ", ".join(c.upper() for c in e.get("countries") or ["EU"])
        notes = _md(" ".join(str(e.get("notes") or "").split()))
        cells = [f"{e.get('label') or e['id']} (`{e['id']}`)", countries, _summary(e["adapter"]),
                 f"{e.get('every_days', 1)}d", str(e.get("runner", "github")), _needs(e), notes]
        sort_key = (countries if _section(e) == "Country boards" else "") + e["id"]
        rows[_section(e)].append((sort_key, "| " + " | ".join(cells) + " |"))
    out = [HEADER]
    for title in ORDER:
        if not rows[title]:
            continue
        out += [f"## {title}\n", "| Source | Covers | How | Every | Runs on | Needs | Notes |",
                "|---|---|---|---|---|---|---|"]
        out += [r for _, r in sorted(rows[title])]
        out.append("")
    return "\n".join(out)
