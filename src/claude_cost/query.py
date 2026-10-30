"""Query service: the one interface the web server and CLI use for every number."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

COST_SQL = ("cost_input + cost_output + cost_cache_write_5m "
            "+ cost_cache_write_1h + cost_cache_read")


@dataclass(frozen=True)
class DaySpend:
    day: date
    cost: float
    requests: int


class QueryService:
    def __init__(self, conn: sqlite3.Connection, timezone: str | None = None):
        self._conn = conn
        self._tz: tzinfo | None = ZoneInfo(timezone) if timezone else None

    def _local_day(self, epoch: float) -> date:
        return datetime.fromtimestamp(epoch, self._tz).date()

    def daily_spend(self, start: date, end: date) -> list[DaySpend]:
        """Cost per local day for start..end inclusive; days without requests are zero."""
        totals: dict[date, list] = {}
        for row in self._conn.execute(f"SELECT ts_epoch, {COST_SQL} AS cost FROM requests"):
            d = self._local_day(row["ts_epoch"])
            if start <= d <= end:
                t = totals.setdefault(d, [0.0, 0])
                t[0] += row["cost"]
                t[1] += 1
        days = (end - start).days + 1
        return [DaySpend(d, *totals.get(d, (0.0, 0)))
                for d in (start + timedelta(n) for n in range(days))]

    def last_ingest_epoch(self) -> float | None:
        """When the store was last refreshed (epoch seconds), None if never."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'last_ingest_epoch'").fetchone()
        return float(row["value"]) if row else None

    def unpriced_models(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT model FROM requests WHERE priced = 0 ORDER BY model")
        return [r["model"] for r in rows]
