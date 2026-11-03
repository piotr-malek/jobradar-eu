"""Generic Algolia search adapter (public search-only keys embedded in the boards' own frontends).

Used for 80,000 Hours and Escape the City; WTTJ can reuse it.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, default_terms, as_list, first, parse_date, to_float

BOARDS: dict[str, dict[str, Any]] = {
    "eighty_k_hours": {
        "app": "W6KM1UDIB3", "key": "d1d7f2c8696e7b36837d5ed337c4a319", "index": "jobs_prod",
        "base_url": "https://jobs.80000hours.org/",
    },
    "escapethecity": {
        "app": "6E1NSXNTTH", "key": "d4ceccfb371537bb6eab4cebd7f33f98", "index": "listings-live",
        "filters": "option-listing-type:Job", "base_url": "https://www.escapethecity.org/opportunity/",
    },
}


def algolia_query(ctx: FetchContext, board: dict[str, Any], query: str, page: int, hits: int = 100) -> dict[str, Any]:
    params = f"hitsPerPage={hits}&page={page}&query={query}"
    if board.get("filters"):
        params += f"&filters={board['filters']}"
    return ctx.http.post_json(
        f"https://{board['app']}-dsn.algolia.net/1/indexes/{board['index']}/query",
        headers={"X-Algolia-Application-Id": board["app"], "X-Algolia-API-Key": board["key"],
                 "Content-Type": "application/json"},
        content=json.dumps({"params": params}),
    )


def _locations(value: Any) -> str:
    out = []
    for loc in as_list(value):
        if isinstance(loc, dict):
            out.append(str(first(loc.get("name"), loc.get("city"), loc.get("country")) or ""))
        else:
            out.append(str(loc))
    return ", ".join(x for x in out if x)


@adapter("algolia_board")
def algolia_board(ctx: FetchContext) -> Iterator[Posting]:
    board = BOARDS[ctx.params["board"]]
    for query in ctx.params.get("queries") or default_terms(2):
        for page in range(int(ctx.params.get("max_pages", 2))):
            data = algolia_query(ctx, board, query, page)
            hits = data.get("hits") or []
            for h in hits:
                jid = str(first(h.get("id"), h.get("objectID"), h.get("slug")))
                url = h.get("url") or (board["base_url"] + str(h.get("slug") or jid))
                remote = h.get("remote")
                yield Posting(
                    source=ctx.spec.id,
                    source_job_id=jid,
                    title=h.get("title") or "",
                    company=first(h.get("company_name"), (h.get("company") or {}).get("name") if isinstance(h.get("company"), dict) else h.get("company")) or "",
                    url=url,
                    location_raw=first(_locations(h.get("locations")), h.get("location")) or "",
                    workplace="remote" if remote is True or (isinstance(remote, list) and remote) else None,
                    description=html_to_text(first(h.get("description"), h.get("description_short"), h.get("headline"))),
                    posted_at=parse_date(first(h.get("posted_at"), h.get("posted_date"), h.get("updated_at"))),
                    salary_min=to_float(h.get("salary_low")),
                    salary_max=to_float(h.get("salary_max")),
                )
            if page + 1 >= int(data.get("nbPages") or 0):
                break
