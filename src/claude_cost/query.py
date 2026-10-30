"""Query service: the one interface the web server and CLI use for every number."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
import calendar
from datetime import date, datetime, time, timedelta, tzinfo
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


PERIOD_MODES = ("last7", "week", "month")
COMPONENTS = (("input", "cost_input"), ("output", "cost_output"),
              ("cache write (5m)", "cost_cache_write_5m"),
              ("cache write (1h)", "cost_cache_write_1h"), ("cache read", "cost_cache_read"))


@dataclass(frozen=True)
class Period:
    start: date  # first local day, inclusive
    end: date    # last local day, inclusive

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


@dataclass(frozen=True)
class ComponentSpend:
    name: str
    current: float
    baseline: float


@dataclass(frozen=True)
class RankedSpend:
    key: str  # Session id or project name
    cost: float
    requests: int


@dataclass(frozen=True)
class PeriodComparison:
    mode: str
    current: Period
    baseline: Period  # same number of elapsed days as `current` (clamped to the month length)
    partial: bool  # the current calendar period has not finished yet
    elapsed_days: int
    period_days: int  # length of the full current period
    current_cost: float
    baseline_cost: float
    components: tuple[ComponentSpend, ...]
    top_sessions: tuple[RankedSpend, ...]
    top_projects: tuple[RankedSpend, ...]

    @property
    def delta_cost(self) -> float:
        return self.current_cost - self.baseline_cost

    @property
    def delta_pct(self) -> float | None:
        """None when the baseline is zero (a percentage is undefined)."""
        return self.delta_cost / self.baseline_cost * 100 if self.baseline_cost else None


def resolve_periods(mode: str, today: date, include_today: bool = True) -> tuple[Period, Period, int]:
    """(current, baseline, full period length). Partial calendar periods compare on equal elapsed days."""
    if mode == "last7":
        end = today if include_today else today - timedelta(days=1)
        return Period(end - timedelta(6), end), Period(end - timedelta(13), end - timedelta(7)), 7
    if mode == "week":
        start = today - timedelta(days=today.weekday())  # Monday
        prev = start - timedelta(days=7)
        elapsed = (today - start).days
        return Period(start, today), Period(prev, prev + timedelta(elapsed)), 7
    if mode == "month":
        start = today.replace(day=1)
        prev = (start - timedelta(days=1)).replace(day=1)
        prev_end = min(prev + timedelta(today.day - 1), (start - timedelta(days=1)))
        return Period(start, today), Period(prev, prev_end), calendar.monthrange(today.year, today.month)[1]
    raise ValueError(f"unknown period mode {mode!r}; expected one of {PERIOD_MODES}")


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

    def period_comparison(self, mode: str, now: datetime, include_today: bool = True,
                          top_n: int = 10) -> PeriodComparison:
        """Spend for a Period against the baseline before it, in the service's time zone.

        `include_today` only affects `last7`; calendar periods always run through today.
        """
        today = now.astimezone(self._tz).date() if self._tz else now.date()
        cur, base, full = resolve_periods(mode, today, include_today)
        cur_sums, base_sums = self._sums(cur), self._sums(base)
        components = tuple(ComponentSpend(name, cur_sums[col], base_sums[col])
                           for name, col in COMPONENTS)
        return PeriodComparison(
            mode, cur, base, cur.days < full and mode != "last7", cur.days, full,
            sum(cur_sums[c] for _, c in COMPONENTS), sum(base_sums[c] for _, c in COMPONENTS),
            components, self._top("COALESCE(l.logical_session_id, r.session_id)", cur, top_n),
            self._top("r.project", cur, top_n))

    def _bounds(self, p: Period) -> tuple[float, float]:
        """Epoch range [start, end) covering the Period's local days."""
        return (datetime.combine(p.start, time.min, self._tz).timestamp(),
                datetime.combine(p.end + timedelta(1), time.min, self._tz).timestamp())

    def _sums(self, p: Period) -> dict[str, float]:
        lo, hi = self._bounds(p)
        cols = ", ".join(f"COALESCE(SUM({c}), 0) AS {c}" for _, c in COMPONENTS)
        row = self._conn.execute(
            f"SELECT {cols} FROM requests WHERE ts_epoch >= ? AND ts_epoch < ?", (lo, hi)).fetchone()
        return {c: row[c] for _, c in COMPONENTS}

    def _top(self, key_sql: str, p: Period, n: int) -> tuple[RankedSpend, ...]:
        lo, hi = self._bounds(p)
        rows = self._conn.execute(
            f"SELECT {key_sql} AS k, SUM({COST_SQL}) AS cost, COUNT(*) AS n "
            "FROM requests r LEFT JOIN session_links l ON l.stem = r.session_id "
            "WHERE r.ts_epoch >= ? AND r.ts_epoch < ? GROUP BY k ORDER BY cost DESC, k LIMIT ?",
            (lo, hi, n)).fetchall()
        return tuple(RankedSpend(r["k"], r["cost"], r["n"]) for r in rows)

    def unpriced_models(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT model FROM requests WHERE priced = 0 ORDER BY model")
        return [r["model"] for r in rows]
