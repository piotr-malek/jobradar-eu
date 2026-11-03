"""Store interface + shared row helpers. Implemented by SQLiteStore (store/sqlite.py)."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Protocol

from jobradar.store.schema import TABLES


def to_jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    return value


def shape_row(table: str, row: dict[str, Any]) -> dict[str, Any]:
    """Keep only schema columns; serialize datetimes / dicts; default repeated → []."""
    fields = TABLES[table][0]
    out: dict[str, Any] = {}
    for name, ftype, mode in fields:
        v = row.get(name)
        if mode == "REPEATED":
            v = [str(x) for x in (v or [])]
        elif ftype == "STRING" and isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False, default=str)
        else:
            v = to_jsonable(v)
        out[name] = v
    return out


class Store(Protocol):
    def init(self) -> None: ...
    def append(self, table: str, rows: list[dict[str, Any]]) -> int: ...
    def known_versions(self, source: str) -> dict[str, str]: ...
    def last_runs(self) -> dict[str, dict[str, Any]]: ...
    def candidates(self, since: datetime, rule_version: str, lookback_days: int = 60) -> list[dict[str, Any]]: ...
    def sent_dedup_keys(self, days: int = 120) -> set[str]: ...
    def digest_sent(self, digest_date: date, kind: str) -> bool: ...
    def last_digest_sent_at(self) -> datetime | None: ...
    def latest_versions(self, since: datetime) -> list[dict[str, Any]]: ...
    def rule_version_known(self, rule_version: str) -> bool: ...
    def ats_registry(self) -> list[dict[str, Any]]: ...
    def ats_poll_summary(self) -> dict[tuple[str, str], dict[str, Any]]: ...
    def digest_items_since(self, since: date) -> list[dict[str, Any]]: ...
    def source_health(self, days: int = 14) -> list[dict[str, Any]]: ...
    def funnel_stats(self, since: datetime) -> dict[str, Any]: ...
