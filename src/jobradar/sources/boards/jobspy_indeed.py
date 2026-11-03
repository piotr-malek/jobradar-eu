"""Indeed via python-jobspy (optional dependency: ``pip install jobradar-eu[jobspy]``).

jobspy does its own HTTP (Indeed's mobile GraphQL API), so PoliteClient's budget and
robots logic do not apply; this adapter paces searches itself (``delay_s``) and keeps
volume tiny (``results_wanted`` per search). jobspy pins an old numpy, so it is not in the
main venv: the adapter raises a clear RuntimeError when it is missing.
"""

from __future__ import annotations

import math
import time
from datetime import date, datetime, timezone
from typing import Any, Iterator

from jobradar.models import Posting
from jobradar.normalize.text import html_to_text
from jobradar.sources.base import FetchContext, adapter, default_terms, as_list, parse_date, to_float
from jobradar.sources.boards.national_apis import country_iso

# jobspy ``country_indeed`` names → ISO-2 (only the ones we are likely to use)
_INDEED_COUNTRY = {
    "poland": "PL", "germany": "DE", "netherlands": "NL", "austria": "AT", "belgium": "BE",
    "switzerland": "CH", "spain": "ES", "france": "FR", "italy": "IT", "uk": "GB", "united kingdom": "GB",
    "ireland": "IE", "portugal": "PT", "sweden": "SE", "denmark": "DK", "norway": "NO", "finland": "FI",
    "czech republic": "CZ", "czechia": "CZ", "romania": "RO", "hungary": "HU", "greece": "GR",
    "luxembourg": "LU", "usa": "US", "us": "US", "united states": "US", "canada": "CA",
}


def _clean(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if str(v) in ("nan", "NaT", "None", ""):
        return None
    return v


def _records(df: Any) -> list[dict[str, Any]]:
    if df is None:
        return []
    if hasattr(df, "to_dict"):
        return [{k: _clean(v) for k, v in r.items()} for r in df.to_dict(orient="records")]
    return [dict(r) for r in df]


def _scrape(**kw: Any) -> Any:
    try:
        from jobspy import scrape_jobs  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised via the test's fake module
        raise RuntimeError("python-jobspy not installed (pip install 'jobradar-eu[jobspy]')") from exc
    return scrape_jobs(**kw)


@adapter("jobspy_indeed")
def jobspy_indeed(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    countries = p.get("countries") or ["poland"]
    terms = p.get("search_terms") or default_terms(2)
    is_remote = bool(p.get("is_remote", False))
    hours_old = int(p.get("hours_old", 72))
    wanted = int(p.get("results_wanted", 30))
    delay = float(p.get("delay_s", 3.0))
    location_override = p.get("location")
    seen: set[str] = set()
    first = True
    for country in countries:
        iso = _INDEED_COUNTRY.get(str(country).lower()) or country_iso(country)
        for term in terms:
            if not first and delay:
                time.sleep(delay)
            first = False
            try:
                df = _scrape(site_name=["indeed"], search_term=term, location=location_override or str(country).title(),
                             country_indeed=str(country), is_remote=is_remote, hours_old=hours_old,
                             results_wanted=wanted, description_format="markdown", verbose=0)
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001 - one failing search does not kill the run
                ctx.notes.append(f"{country}/{term}: {type(exc).__name__}: {str(exc)[:120]}")
                continue
            for r in _records(df):
                jid = str(r.get("id") or r.get("job_url") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                interval = (r.get("interval") or "") or None
                period = {"yearly": "year", "monthly": "month", "weekly": "week", "daily": "day",
                          "hourly": "hour"}.get(str(interval).lower()) if interval else None
                posted = r.get("date_posted")
                if isinstance(posted, date) and not isinstance(posted, datetime):
                    posted = datetime(posted.year, posted.month, posted.day, tzinfo=timezone.utc)
                yield Posting(
                    source=ctx.spec.id,
                    source_job_id=jid,
                    title=r.get("title") or "",
                    company=r.get("company") or "",
                    url=r.get("job_url") or "",
                    apply_url=r.get("job_url_direct") or None,
                    location_raw=r.get("location") or "",
                    countries=[iso] if iso else [],  # Indeed country site searched (structured)
                    workplace="remote" if r.get("is_remote") is True else None,
                    description=html_to_text(r.get("description")),
                    posted_at=parse_date(posted),
                    employment_types=[x.strip() for x in str(r.get("job_type") or "").split(",") if x.strip()],
                    salary_min=to_float(r.get("min_amount")),
                    salary_max=to_float(r.get("max_amount")),
                    salary_currency=r.get("currency") if r.get("min_amount") or r.get("max_amount") else None,
                    salary_period=period if r.get("min_amount") or r.get("max_amount") else None,
                    tags=[x for x in as_list(r.get("company_industry")) if x],
                    raw={"site": r.get("site"), "search": term, "country_indeed": country,
                         "job_level": r.get("job_level")},
                )
