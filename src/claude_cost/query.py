"""Query service: the one interface the web server and CLI use for every number."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

from . import pricing

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


@dataclass(frozen=True)
class PriceRow:
    model: str  # model-name prefix
    effective_from: date
    input: float  # $ per million tokens
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    tier_note: str  # e.g. "x2.0 above 100000 prompt tokens", "" when flat
    source: str  # "default" or "config"


class QueryService:
    def __init__(self, conn: sqlite3.Connection, timezone: str | None = None,
                 price_table: dict | None = None):
        self._conn = conn
        self._table = pricing.PRICE_TABLE if price_table is None else price_table
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

    def price_table(self) -> list[PriceRow]:
        """The price table in effect (defaults plus config overrides), $ per million tokens."""
        rows = []
        for model in sorted(self._table):
            for e in sorted(self._table[model], key=lambda e: e.effective_from):
                note = (f"x{e.tier_mult:g} above {e.tier_threshold} prompt tokens"
                        if e.tier_threshold else "")
                rows.append(PriceRow(model, e.effective_from, e.input, e.output,
                                     e.input * pricing.WRITE_5M_MULT, e.input * pricing.WRITE_1H_MULT,
                                     e.cache_read, note, e.source))
        return rows

    def unpriced_models(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT model FROM requests WHERE priced = 0 ORDER BY model")
        return [r["model"] for r in rows]
