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


@dataclass(frozen=True)
class SessionSummary:
    session_id: str  # the logical Session: its earliest transcript's id
    cost: float
    requests: int
    transcripts: tuple[str, ...]  # every transcript grouped into this Session


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
    def session_summaries(self) -> list[SessionSummary]:
        """One row per Session (logical conversation), oldest first."""
        rows = self._conn.execute(
            f"SELECT COALESCE(l.logical_session_id, r.session_id) AS sid, "
            f"SUM({COST_SQL}) AS cost, COUNT(*) AS n, MIN(r.ts_epoch) AS first "
            "FROM requests r LEFT JOIN session_links l ON l.stem = r.session_id "
            "GROUP BY sid ORDER BY first, sid").fetchall()
        members: dict[str, list[str]] = {}
        for r in self._conn.execute(
                "SELECT stem, logical_session_id FROM session_links ORDER BY stem"):
            members.setdefault(r["logical_session_id"], []).append(r["stem"])
        return [SessionSummary(r["sid"], r["cost"], r["n"],
                               tuple(members.get(r["sid"], [r["sid"]]))) for r in rows]

    def unpriced_models(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT model FROM requests WHERE priced = 0 ORDER BY model")
        return [r["model"] for r in rows]
