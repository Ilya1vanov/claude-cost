"""Query service: the one interface the web server and CLI use for every number."""
from __future__ import annotations

import dataclasses
import sqlite3
from dataclasses import dataclass
import calendar
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from . import compact as compact_mod
from . import explore
from . import findings as findings_mod
from . import pricing, spotlights

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
    # cost/requests above include the Session's Subagents; these are the Subagent part
    subagent_cost: float = 0.0
    subagent_requests: int = 0
    # Side session / Fork link to the parent Session (None = top-level)
    parent_session_id: str | None = None
    link_kind: str | None = None  # "side" | "fork"


@dataclass(frozen=True)
class SubagentSummary:
    session_id: str  # the parent logical Session
    agent_id: str
    agent_type: str | None  # None when the metadata file is missing
    is_fork: bool
    tool_use_id: str | None  # id of the parent's Agent tool call (None for forks and nested agents)
    parent_agent_id: str | None  # set on nested Subagents
    description: str | None
    cost: float
    requests: int


@dataclass(frozen=True)
class SpotlightSession:
    session_id: str
    label: str
    cost: float  # includes the Session's Subagents
    subagent_cost: float
    requests: int
    link_kind: str | None  # "side" | "fork" | None
    also_in: tuple[str, ...]  # other Spotlights that count this Session fully too
    pinned: bool


@dataclass(frozen=True)
class SpotlightRelated:
    """A Fork of a member that is not counted ("related, excluded")."""
    session_id: str
    label: str
    cost: float
    parent_id: str


@dataclass(frozen=True)
class SpotlightDay:
    day: date
    cost: float
    sessions: dict[str, float]    # Session id -> cost that day (Subagents included)
    subagents: dict[str, float]   # "<Session id>/<agent id>" -> cost that day (a subset of sessions)
    components: dict[str, float]  # Cost component name -> cost that day


@dataclass(frozen=True)
class SpotlightSubagent:
    session_id: str
    agent_id: str
    agent_type: str | None
    description: str | None
    cost: float


@dataclass(frozen=True)
class SpotlightSummary:
    name: str
    kind: str  # "auto" | "rule" | "pinned"
    rule: str  # regex, "" for pinned; the key for an Auto-spotlight
    total: float  # lifetime cost; Spotlight totals overlap and must never be added together
    requests: int
    sessions: int
    first_activity: date | None
    last_activity: date | None


@dataclass(frozen=True)
class SpotlightRank:
    """A dashboard row: spend inside the Period, next to the lifetime figures."""
    summary: SpotlightSummary
    period_cost: float


@dataclass(frozen=True)
class SpotlightDetail:
    summary: SpotlightSummary
    sessions: tuple[SpotlightSession, ...]
    subagents: tuple[SpotlightSubagent, ...]
    related: tuple[SpotlightRelated, ...]
    days: tuple[SpotlightDay, ...]  # lifetime, first to last activity, days without spend included
    components: dict[str, float]  # lifetime cost per Cost component
    pins: tuple[str, ...]

IDLE_GAP_SECONDS = 300  # a gap longer than the 5m cache TTL is marked as an idle gap


@dataclass(frozen=True)
class TimelinePoint:
    message_id: str
    ts: float  # epoch seconds
    model: str
    context_tokens: int  # prompt size: input + cache read + cache writes
    cost: float
    gap_seconds: float | None  # idle time since the previous request (None for the first)
    idle: bool  # gap_seconds > IDLE_GAP_SECONDS
    cache_event: str  # cold | warm | none (a Cold write is marked)
    threshold: int | None = None  # Compact threshold of this request's model (None: unknown)


@dataclass(frozen=True)
class CompactionMark:
    ts: float
    trigger: str  # manual | auto
    pre_tokens: int
    post_tokens: int
    before_index: int  # number of timeline points before the compaction
    threshold: int | None = None  # Compact threshold of the model it ran on
    late: bool = False  # Late compact: started above the threshold


@dataclass(frozen=True)
class SubagentLaunch:
    agent_id: str
    agent_type: str | None
    description: str | None
    is_fork: bool
    ts: float  # its first request
    before_index: int
    cost: float
    requests: int  # its own timeline: SessionTimeline(session_id, agent_id=agent_id)


@dataclass(frozen=True)
class SideSessionMark:
    session_id: str  # the Side session's own timeline
    ts: float
    before_index: int
    cost: float
    requests: int


@dataclass(frozen=True)
class SessionTimeline:
    """How a Session's (or one Subagent's) cost built up, request by request."""
    session_id: str
    agent_id: str | None  # set for a Subagent's timeline
    cost: float  # of the requests on this timeline
    points: tuple[TimelinePoint, ...]
    compactions: tuple[CompactionMark, ...]
    subagents: tuple[SubagentLaunch, ...]  # launched from this timeline (direct children)
    side_sessions: tuple[SideSessionMark, ...]
    parent_session_id: str | None = None  # of a Side session / Fork
    link_kind: str | None = None


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


@dataclass(frozen=True)
class CacheStats:
    """Cache behaviour over one Period. Costs are $; hit ratio is by tokens (None if no tokens)."""
    hit_ratio: float | None  # cache read / (cache read + cache write + uncached input)
    cold_cost: float
    warm_cost: float
    write_5m_cost: float
    write_1h_cost: float
    rebuilds: int  # requests whose write was Cold


@dataclass(frozen=True)
class CacheCard:
    mode: str
    current: CacheStats
    baseline: CacheStats

    @property
    def delta_hit_ratio(self) -> float | None:
        a, b = self.current.hit_ratio, self.baseline.hit_ratio
        return None if a is None or b is None else a - b


@dataclass(frozen=True)
class CacheEventSpend:
    event: str  # "cold write" | "warm write" | "cache read"
    cost: float
    tokens: int
    requests: int


COMPARISON_MODES = ("week", "month")


@dataclass(frozen=True)
class ComparisonEntry:
    period: Period  # the full calendar period
    cost: float
    daily: list[float]  # cost per day of period; stops at today for a period still running
    partial: bool
    delta_cost: float  # vs the Baseline (0 for the Baseline itself)
    delta_pct: float | None  # None when the Baseline cost is zero


@dataclass(frozen=True)
class Comparison:
    mode: str
    baseline: date  # start of the Baseline period
    entries: tuple[ComparisonEntry, ...]  # earliest first
    day_count: int  # length of the longest selected period (the chart's x axis)


def calendar_period(mode: str, start: date) -> Period:
    """The full calendar week (Monday start) or month beginning at `start`."""
    if mode == "week" and start.weekday() == 0:
        return Period(start, start + timedelta(6))
    if mode == "month" and start.day == 1:
        return Period(start, start.replace(day=calendar.monthrange(start.year, start.month)[1]))
    raise ValueError(f"{start} is not the start of a {mode}")


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
                 price_table: dict | None = None, thresholds: dict[str, int] | None = None,
                 key_pattern: str | None = None):
        self._conn = conn
        self._thresholds = thresholds or {}
        self._key_pattern = key_pattern
        self._table = pricing.PRICE_TABLE if price_table is None else price_table
        self._tz: tzinfo | None = ZoneInfo(timezone) if timezone else None

    def _local_day(self, epoch: float) -> date:
        return datetime.fromtimestamp(epoch, self._tz).date()

    @staticmethod
    def _filter(surface: str | None, data_root: str | Path | None) -> tuple[str, list]:
        """WHERE clause (over `requests r`) for the Surface and Data root filters."""
        where, args = [], []
        if surface is not None:
            where.append("r.message_id IN (SELECT message_id FROM request_surface WHERE surface = ?)")
            args.append(surface)
        if data_root is not None:
            where.append("r.data_root = ?")
            args.append(str(data_root))
        return (" WHERE " + " AND ".join(where)) if where else "", args

    def daily_spend(self, start: date, end: date, surface: str | None = None,
                    data_root: str | Path | None = None) -> list[DaySpend]:
        """Cost per local day for start..end inclusive; days without requests are zero.

        `surface` ("CLI", "Desktop", "SDK") and `data_root` restrict the requests counted.
        """
        totals: dict[date, list] = {}
        where, args = self._filter(surface, data_root)
        for row in self._conn.execute(
                f"SELECT r.ts_epoch, {COST_SQL} AS cost FROM requests r{where}", args):
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

    def session_summaries(self, surface: str | None = None,
                          data_root: str | Path | None = None) -> list[SessionSummary]:
        """One row per Session (logical conversation), oldest first.

        Costs include the Session's Subagents. Side sessions and Forks carry their parent
        Session's id when the Desktop store linked them; everything else is top-level.
        """
        where, args = self._filter(surface, data_root)
        rows = self._conn.execute(
            f"SELECT COALESCE(l.logical_session_id, r.session_id) AS sid, "
            f"SUM({COST_SQL}) AS cost, COUNT(*) AS n, MIN(r.ts_epoch) AS first, "
            f"SUM(CASE WHEN a.agent_id IS NOT NULL THEN {COST_SQL} ELSE 0 END) AS sub_cost, "
            f"SUM(a.agent_id IS NOT NULL) AS sub_n "
            "FROM requests r LEFT JOIN session_links l ON l.stem = r.session_id "
            f"LEFT JOIN request_surface a ON a.message_id = r.message_id{where} "
            "GROUP BY sid ORDER BY first, sid", args).fetchall()
        members: dict[str, list[str]] = {}
        logical: dict[str, str] = {}
        for r in self._conn.execute(
                "SELECT stem, logical_session_id FROM session_links ORDER BY stem"):
            members.setdefault(r["logical_session_id"], []).append(r["stem"])
            logical[r["stem"]] = r["logical_session_id"]
        parents: dict[str, tuple[str, str]] = {}
        for rel in self._conn.execute(
                "SELECT child_stem, parent_stem, kind FROM session_relations ORDER BY child_stem"):
            child = logical.get(rel["child_stem"], rel["child_stem"])
            parent = logical.get(rel["parent_stem"], rel["parent_stem"])
            if child != parent:
                parents.setdefault(child, (parent, rel["kind"]))
        return [SessionSummary(r["sid"], r["cost"], r["n"],
                               tuple(members.get(r["sid"], [r["sid"]])),
                               r["sub_cost"], r["sub_n"] or 0,
                               *parents.get(r["sid"], (None, None))) for r in rows]

    def subagent_summaries(self, surface: str | None = None,
                           data_root: str | Path | None = None) -> list[SubagentSummary]:
        """One row per Subagent that made requests, with its own cost, oldest first."""
        where, args = self._filter(surface, data_root)
        rows = self._conn.execute(
            f"SELECT COALESCE(l.logical_session_id, r.session_id) AS sid, a.agent_id AS agent_id, "
            "s.agent_type, COALESCE(s.is_fork, 0) AS is_fork, s.tool_use_id, s.parent_agent_id, "
            f"s.description, SUM({COST_SQL}) AS cost, COUNT(*) AS n, MIN(r.ts_epoch) AS first "
            "FROM requests r JOIN request_surface a ON a.message_id = r.message_id "
            "LEFT JOIN session_links l ON l.stem = r.session_id "
            "LEFT JOIN (SELECT f.file_stem AS stem, sf.agent_id, MAX(sf.agent_type) AS agent_type, "
            "MAX(sf.is_fork) AS is_fork, MAX(sf.tool_use_id) AS tool_use_id, "
            "MAX(sf.parent_agent_id) AS parent_agent_id, MAX(sf.description) AS description "
            "FROM subagent_files sf JOIN ingest_files f ON f.path = sf.path "
            "GROUP BY f.file_stem, sf.agent_id) s ON s.stem = r.session_id AND s.agent_id = a.agent_id "
            f"{where}{' AND' if where else ' WHERE'} a.agent_id IS NOT NULL "
            "GROUP BY r.session_id, a.agent_id ORDER BY first, sid, a.agent_id", args).fetchall()
        return [SubagentSummary(r["sid"], r["agent_id"], r["agent_type"], bool(r["is_fork"]),
                                r["tool_use_id"], r["parent_agent_id"], r["description"],
                                r["cost"], r["n"]) for r in rows]

    def compact_thresholds(self) -> compact_mod.CompactAnalysis:
        """Compact threshold per model (global fallback), with cost-vs-T curve and payback."""
        return compact_mod.analyze(self._conn, self._tz, self._table)

    def session_timeline(self, session_id: str, agent_id: str | None = None) -> SessionTimeline | None:
        """Timeline of a Session's own requests (its Subagents are separate, marked as launches),
        or of one Subagent with `agent_id`. None when the Session or Subagent is unknown."""
        rows = self._conn.execute(
            f"SELECT r.message_id, r.ts_epoch, r.model, r.input_tokens + r.cache_read_tokens "
            "+ r.cache_write_5m_tokens + r.cache_write_1h_tokens AS ctx, "
            f"{COST_SQL} AS cost, COALESCE(c.event, 'none') AS event "
            "FROM requests r LEFT JOIN session_links l ON l.stem = r.session_id "
            "LEFT JOIN request_surface a ON a.message_id = r.message_id "
            "LEFT JOIN request_cache c ON c.message_id = r.message_id "
            "WHERE COALESCE(l.logical_session_id, r.session_id) = ? AND a.agent_id IS ? "
            "ORDER BY r.ts_epoch, r.message_id", (session_id, agent_id)).fetchall()
        if not rows:
            return None
        points, prev = [], None
        for r in rows:
            gap = None if prev is None else r["ts_epoch"] - prev
            prev = r["ts_epoch"]
            points.append(TimelinePoint(r["message_id"], r["ts_epoch"], r["model"], r["ctx"],
                                        r["cost"], gap, gap is not None and gap > IDLE_GAP_SECONDS,
                                        r["event"]))
        analysis = self.compact_thresholds()

        def threshold(model: str) -> int | None:
            t = analysis.for_model(model)
            return t.threshold if t else None
        points = [dataclasses.replace(p, threshold=threshold(p.model)) for p in points]
        times = [p.ts for p in points]

        def index(ts: float) -> int:
            return sum(1 for t in times if t < ts)

        stems = [r["stem"] for r in self._conn.execute(
            "SELECT stem FROM session_links WHERE logical_session_id = ?", (session_id,))] or [session_id]
        marks = ",".join("?" * len(stems))
        compactions = tuple(self._mark(c, index(c["ts_epoch"]), points)
                            for c in self._conn.execute(
                                f"SELECT * FROM compactions WHERE stem IN ({marks}) "
                                "ORDER BY ts_epoch, uuid", stems)) if agent_id is None else ()
        launches = tuple(
            SubagentLaunch(a.agent_id, a.agent_type, a.description, a.is_fork, first, index(first),
                           a.cost, a.requests)
            for a, first in self._subagent_starts(session_id) if a.parent_agent_id == agent_id)
        sides = tuple(SideSessionMark(s.session_id, first, index(first), s.cost, s.requests)
                      for s, first in self._side_sessions(session_id)) if agent_id is None else ()
        parent = next((s for s in self.session_summaries() if s.session_id == session_id), None)
        return SessionTimeline(
            session_id, agent_id, sum(p.cost for p in points), tuple(points), compactions, launches,
            sides, parent.parent_session_id if parent and agent_id is None else None,
            parent.link_kind if parent and agent_id is None else None)

    @staticmethod
    def _mark(c, before: int, points: list[TimelinePoint]) -> CompactionMark:
        t = points[before - 1].threshold if before else None  # the model it ran on
        return CompactionMark(c["ts_epoch"], c["trigger"], c["pre_tokens"], c["post_tokens"],
                              before, t, t is not None and c["pre_tokens"] > t)

    def _first_ts(self, session_id: str, agent_id: str | None = None) -> float:
        return self._conn.execute(
            "SELECT MIN(r.ts_epoch) FROM requests r LEFT JOIN session_links l ON l.stem = r.session_id "
            "LEFT JOIN request_surface a ON a.message_id = r.message_id "
            "WHERE COALESCE(l.logical_session_id, r.session_id) = ? AND a.agent_id IS ?",
            (session_id, agent_id)).fetchone()[0]

    def _subagent_starts(self, session_id: str) -> list[tuple[SubagentSummary, float]]:
        return [(a, self._first_ts(session_id, a.agent_id))
                for a in self.subagent_summaries() if a.session_id == session_id]

    def _side_sessions(self, session_id: str) -> list[tuple[SessionSummary, float]]:
        return [(s, self._first_ts(s.session_id)) for s in self.session_summaries()
                if s.parent_session_id == session_id and s.link_kind == "side"]

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

    def cache_events(self) -> dict[str, str]:
        """Cache write event per request: message id -> 'cold' | 'warm' | 'none' (no write)."""
        return {r["message_id"]: r["event"]
                for r in self._conn.execute("SELECT message_id, event FROM request_cache")}

    def _cache_stats(self, p: Period) -> CacheStats:
        lo, hi = self._bounds(p)
        row = self._conn.execute(
            "SELECT COALESCE(SUM(r.cache_read_tokens), 0) AS rd, "
            "COALESCE(SUM(r.cache_write_5m_tokens + r.cache_write_1h_tokens), 0) AS wr, "
            "COALESCE(SUM(r.input_tokens), 0) AS inp, "
            "COALESCE(SUM(CASE WHEN c.event = 'cold' THEN r.cost_cache_write_5m + r.cost_cache_write_1h END), 0) AS cold, "
            "COALESCE(SUM(CASE WHEN c.event = 'warm' THEN r.cost_cache_write_5m + r.cost_cache_write_1h END), 0) AS warm, "
            "COALESCE(SUM(r.cost_cache_write_5m), 0) AS w5, COALESCE(SUM(r.cost_cache_write_1h), 0) AS w1, "
            "COALESCE(SUM(c.event = 'cold'), 0) AS rebuilds "
            "FROM requests r LEFT JOIN request_cache c ON c.message_id = r.message_id "
            "WHERE r.ts_epoch >= ? AND r.ts_epoch < ?", (lo, hi)).fetchone()
        total = row["rd"] + row["wr"] + row["inp"]
        return CacheStats(row["rd"] / total if total else None, row["cold"], row["warm"],
                          row["w5"], row["w1"], row["rebuilds"])

    def cache_card(self, mode: str, now: datetime, include_today: bool = True) -> CacheCard:
        """Cache metrics for the Period against its baseline (same periods as period_comparison)."""
        today = now.astimezone(self._tz).date() if self._tz else now.date()
        cur, base, _ = resolve_periods(mode, today, include_today)
        return CacheCard(mode, self._cache_stats(cur), self._cache_stats(base))

    def spend_by_cache_event(self, start: date, end: date) -> list[CacheEventSpend]:
        """Cache event dimension: cost, tokens and requests per event over local days start..end."""
        lo, hi = self._bounds(Period(start, end))
        rows = self._conn.execute(
            "SELECT c.event AS ev, COUNT(*) AS n, "
            "SUM(r.cost_cache_write_5m + r.cost_cache_write_1h) AS cost, "
            "SUM(r.cache_write_5m_tokens + r.cache_write_1h_tokens) AS tok "
            "FROM requests r JOIN request_cache c ON c.message_id = r.message_id "
            "WHERE r.ts_epoch >= ? AND r.ts_epoch < ? AND c.event IN ('cold', 'warm') "
            "GROUP BY c.event", (lo, hi)).fetchall()
        out = [CacheEventSpend(f"{r['ev']} write", r["cost"], r["tok"], r["n"]) for r in rows]
        rd = self._conn.execute(
            "SELECT SUM(cost_cache_read) AS cost, SUM(cache_read_tokens) AS tok, COUNT(*) AS n "
            "FROM requests WHERE ts_epoch >= ? AND ts_epoch < ? AND cache_read_tokens > 0",
            (lo, hi)).fetchone()
        if rd["n"]:
            out.append(CacheEventSpend("cache read", rd["cost"], rd["tok"], rd["n"]))
        return sorted(out, key=lambda e: e.event)


    def findings(self, since: date | None = None,
                 until: date | None = None) -> list[findings_mod.Finding]:
        """Every Drain, then every Opportunity, over requests from local day `since` through
        `until` (all history when None). Findings overlap: never sum their avoidable figures."""
        lo = datetime.combine(since, time.min, self._tz).timestamp() if since else None
        hi = datetime.combine(until + timedelta(1), time.min, self._tz).timestamp() if until else None
        rows = findings_mod.load_rows(self._conn, self._tz, lo, hi)
        by_id = {r.message_id: r for r in rows}
        defs = findings_mod.all_findings()
        ctx = findings_mod.FindingContext(rows, self._thresholds, findings_mod.all_thresholds(),
                                          self._table, self._conn,
                                          compact_mod.analyze(self._conn, self._tz, self._table))
        return [findings_mod.build_finding(d, d.detect(ctx), by_id) for d in defs]

    def top_findings(self, mode: str, now: datetime, include_today: bool = True,
                     top_n: int = 10) -> list[findings_mod.Finding]:
        """The findings covering the most spend inside the Period (same periods as
        period_comparison), costliest first; findings that cover nothing are left out."""
        cur, _, _ = resolve_periods(mode, self._today(now), include_today)
        hits = [f for f in self.findings(cur.start, cur.end) if f.attributed > 0]
        return sorted(hits, key=lambda f: (-f.attributed, f.id))[:top_n]

    def finding(self, finding_id: str, since: date | None = None) -> findings_mod.Finding:
        for f in self.findings(since):
            if f.id == finding_id:
                return f
        raise KeyError(finding_id)

    def available_periods(self, mode: str, now: datetime) -> list[date]:
        """Start dates of every calendar period from the first request to now, newest first."""
        if mode not in COMPARISON_MODES:
            raise ValueError(f"mode must be one of {COMPARISON_MODES}")
        row = self._conn.execute("SELECT MIN(ts_epoch) AS first FROM requests").fetchone()
        if row["first"] is None:
            return []
        today = self._today(now)
        first = self._local_day(row["first"])
        if mode == "week":
            cur, step = first - timedelta(days=first.weekday()), lambda d: d + timedelta(7)
        else:
            cur = first.replace(day=1)
            step = lambda d: (d.replace(day=28) + timedelta(4)).replace(day=1)
        out = []
        while cur <= today:
            out.append(cur)
            cur = step(cur)
        return out[::-1]

    def comparison(self, mode: str, starts: list[date], now: datetime,
                   baseline: date | None = None) -> Comparison:
        """Two or more calendar periods of one mode, against a Baseline (default: the earliest)."""
        if mode not in COMPARISON_MODES:
            raise ValueError(f"mode must be one of {COMPARISON_MODES}, not {mode!r}")
        starts = sorted(set(starts))
        if len(starts) < 2:
            raise ValueError("select at least two different periods")
        today = self._today(now)
        periods = [calendar_period(mode, s) for s in starts]
        if periods[-1].start > today:
            raise ValueError(f"{periods[-1].start} has not started yet")
        baseline = baseline or starts[0]
        if baseline not in starts:
            raise ValueError("the baseline must be one of the selected periods")
        costs, dailies = [], []
        for p in periods:
            daily = [d.cost for d in self.daily_spend(p.start, min(p.end, today))]
            dailies.append(daily)
            costs.append(sum(daily))
        base_cost = costs[starts.index(baseline)]
        entries = tuple(
            ComparisonEntry(p, c, d, p.end > today, c - base_cost,
                            (c - base_cost) / base_cost * 100 if base_cost else None)
            for p, c, d in zip(periods, costs, dailies))
        return Comparison(mode, baseline, entries, max(p.days for p in periods))

    def _today(self, now: datetime) -> date:
        return now.astimezone(self._tz).date() if self._tz else now.date()

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

    # ---- Spotlights (ticket #13) -------------------------------------------------------

    def _session_infos(self) -> dict[str, spotlights.SessionInfo]:
        """Every logical Session with the fields Spotlights search and its Side/Fork link."""
        summaries = self.session_summaries()
        logical = {r["stem"]: r["logical_session_id"]
                   for r in self._conn.execute("SELECT stem, logical_session_id FROM session_links")}
        meta = {r["stem"]: r for r in self._conn.execute("SELECT * FROM session_meta")}
        # A transcript that holds copied history (forkedFrom) is a Fork of the copied Session.
        fork_of: dict[str, str] = {}
        for r in self._conn.execute(
                "SELECT DISTINCT f.file_stem AS stem, c.session_id AS orig FROM request_copies c "
                "JOIN ingest_files f ON f.path = c.path "
                "WHERE c.is_copy = 1 AND c.path NOT IN (SELECT path FROM subagent_files)"):
            child, parent = logical.get(r["stem"], r["stem"]), logical.get(r["orig"], r["orig"])
            if child != parent:
                fork_of.setdefault(child, parent)
        out = {}
        for s in summaries:
            rows = [meta[t] for t in s.transcripts if t in meta]
            parent, kind = s.parent_session_id, s.link_kind
            if kind is None and s.session_id in fork_of:
                parent, kind = fork_of[s.session_id], "fork"

            def vals(col: str, rows=rows) -> tuple[str, ...]:
                return tuple(r[col] for r in rows if r[col])
            out[s.session_id] = spotlights.SessionInfo(
                s.session_id, vals("custom_title"), vals("git_branch"), vals("cwd"),
                vals("first_prompt"), parent, kind)
        return out

    def _key_regex(self):
        return spotlights.compile_pattern(self._key_pattern, "spotlight_key_pattern") \
            if self._key_pattern else None

    def session_keys(self) -> dict[str, list[str]]:
        """Keys the configured pattern finds per Session, in order: title, git branch, worktree
        path, first prompt (all collected). Sessions without keys are left out."""
        pat = self._key_regex()
        keys = {sid: spotlights.detect_keys(info, pat) for sid, info in self._session_infos().items()}
        return {sid: k for sid, k in keys.items() if k}

    def _definitions(self, infos: dict[str, spotlights.SessionInfo],
                     keys: dict[str, list[str]]) -> list[spotlights.Definition]:
        """User-defined Spotlights, then an Auto-spotlight per key (unless a user-defined one has the name)."""
        logical = {r["stem"]: r["logical_session_id"]
                   for r in self._conn.execute("SELECT stem, logical_session_id FROM session_links")}
        pins: dict[str, set[str]] = {}
        for r in self._conn.execute("SELECT spotlight, session_id FROM spotlight_pins"):
            pins.setdefault(r["spotlight"], set()).add(logical.get(r["session_id"], r["session_id"]))
        defs = [spotlights.Definition(r["name"], "rule" if r["rule"] else "pinned", r["rule"],
                                      frozenset(pins.get(r["name"], ())))
                for r in self._conn.execute("SELECT name, rule FROM spotlights ORDER BY name")]
        taken = {d.name for d in defs}
        found = sorted({k for ks in keys.values() for k in ks} - taken)
        return defs + [spotlights.Definition(k, "auto", k, frozenset(pins.get(k, ()))) for k in found]

    def _spotlight_state(self):
        infos = self._session_infos()
        pat = self._key_regex()
        keys = {sid: spotlights.detect_keys(info, pat) for sid, info in infos.items()}
        defs = self._definitions(infos, keys)
        return infos, defs, {d.name: spotlights.resolve(d, infos, keys) for d in defs}

    def _request_rows(self) -> list:
        return self._conn.execute(
            "SELECT COALESCE(l.logical_session_id, r.session_id) AS sid, a.agent_id AS agent, "
            "r.ts_epoch AS ts, r.cost_input, r.cost_output, r.cost_cache_write_5m, "
            "r.cost_cache_write_1h, r.cost_cache_read "
            "FROM requests r LEFT JOIN session_links l ON l.stem = r.session_id "
            "LEFT JOIN request_surface a ON a.message_id = r.message_id").fetchall()

    def _summary(self, defn, members: frozenset[str], rows: list) -> SpotlightSummary:
        mine = [r for r in rows if r["sid"] in members]
        times = [r["ts"] for r in mine]
        return SpotlightSummary(
            defn.name, defn.kind, defn.rule, sum(sum(r[c] for _, c in COMPONENTS) for r in mine),
            len(mine), len(members), self._local_day(min(times)) if times else None,
            self._local_day(max(times)) if times else None)

    def spotlights(self) -> list[SpotlightSummary]:
        """Every Spotlight with its lifetime total and first and last activity, costliest first.

        Totals overlap when a Session is in several Spotlights: never add them together.
        """
        _, defs, member = self._spotlight_state()
        rows = self._request_rows()
        out = [self._summary(d, member[d.name].members, rows) for d in defs]
        return sorted(out, key=lambda s: (-s.total, s.name))

    def top_spotlights(self, mode: str, now: datetime, include_today: bool = True,
                       top_n: int = 10) -> list[SpotlightRank]:
        """Spotlights ranked by spend inside the Period (same periods as period_comparison)."""
        cur, _, _ = resolve_periods(mode, self._today(now), include_today)
        lo, hi = self._bounds(cur)
        _, defs, member = self._spotlight_state()
        rows = self._request_rows()
        ranked = []
        for d in defs:
            members = member[d.name].members
            period_cost = sum(sum(r[c] for _, c in COMPONENTS) for r in rows
                              if r["sid"] in members and lo <= r["ts"] < hi)
            if period_cost > 0:
                ranked.append(SpotlightRank(self._summary(d, members, rows), period_cost))
        ranked.sort(key=lambda x: (-x.period_cost, x.summary.name))
        return ranked[:top_n]

    def spotlight(self, name: str) -> SpotlightDetail:
        """One Spotlight in full; KeyError if there is none of that name."""
        infos, defs, member = self._spotlight_state()
        defn = next((d for d in defs if d.name == name), None)
        if defn is None:
            raise KeyError(name)
        ms = member[name]
        rows = self._request_rows()
        summary = self._summary(defn, ms.members, rows)
        in_other: dict[str, list[str]] = {}
        for other, m in member.items():
            if other != name:
                for sid in m.members & ms.members:
                    in_other.setdefault(sid, []).append(other)
        by = {s.session_id: s for s in self.session_summaries()}
        sessions = tuple(sorted(
            (SpotlightSession(sid, infos[sid].label, by[sid].cost, by[sid].subagent_cost,
                              by[sid].requests, infos[sid].kind, tuple(sorted(in_other.get(sid, ()))),
                              sid in defn.pins)
             for sid in ms.members if sid in by), key=lambda x: (-x.cost, x.session_id)))
        related = tuple(sorted(
            (SpotlightRelated(sid, infos[sid].label, by[sid].cost if sid in by else 0.0,
                              infos[sid].parent or "") for sid in ms.related),
            key=lambda x: (-x.cost, x.session_id)))
        subs = tuple(SpotlightSubagent(a.session_id, a.agent_id, a.agent_type, a.description, a.cost)
                     for a in self.subagent_summaries() if a.session_id in ms.members)
        days: dict[date, list] = {}
        comps = {n: 0.0 for n, _ in COMPONENTS}
        for r in rows:
            if r["sid"] not in ms.members:
                continue
            d = days.setdefault(self._local_day(r["ts"]), [0.0, {}, {}, {n: 0.0 for n, _ in COMPONENTS}])
            total = 0.0
            for n, c in COMPONENTS:
                d[3][n] += r[c]
                comps[n] += r[c]
                total += r[c]
            d[0] += total
            d[1][r["sid"]] = d[1].get(r["sid"], 0.0) + total
            if r["agent"] is not None:
                key = f"{r['sid']}/{r['agent']}"
                d[2][key] = d[2].get(key, 0.0) + total
        out_days = ()
        if days:
            first, last = min(days), max(days)
            out_days = tuple(
                SpotlightDay(day, *(days[day] if day in days else (0.0, {}, {}, {n: 0.0 for n, _ in COMPONENTS})))
                for day in (first + timedelta(n) for n in range((last - first).days + 1)))
        return SpotlightDetail(summary, sessions, subs, related, out_days, comps,
                               tuple(sorted(defn.pins)))

    def spotlight_comparison(self, names: list[str]) -> list[SpotlightDetail]:
        """Two or more Spotlights side by side, each in full (never summed). KeyError if unknown."""
        names = list(dict.fromkeys(names))
        if len(names) < 2:
            raise ValueError("select at least two different Spotlights")
        return [self.spotlight(n) for n in names]

    # Editing user-defined Spotlights (rules and pins live in the store, not config.toml).

    def save_spotlight(self, name: str, rule: str = "") -> None:
        """Create a Spotlight or change its rule ("" = pinned Sessions only)."""
        name, rule = name.strip(), rule.strip()
        if not name:
            raise spotlights.SpotlightError("a Spotlight needs a name")
        if rule:
            spotlights.compile_pattern(rule, "rule")
        with self._conn:
            self._conn.execute(
                "INSERT INTO spotlights VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET rule = excluded.rule",
                (name, rule))

    def delete_spotlight(self, name: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM spotlights WHERE name = ?", (name,))
            self._conn.execute("DELETE FROM spotlight_pins WHERE spotlight = ?", (name,))

    def pin_session(self, name: str, session_id: str) -> None:
        """Pin a Session to a Spotlight: an Auto-spotlight of that name, else a user-defined one
        (created if it does not exist yet)."""
        name = name.strip()
        if not name:
            raise spotlights.SpotlightError("a Spotlight needs a name")
        is_auto = any(name in ks for ks in self.session_keys().values())
        row = self._conn.execute("SELECT logical_session_id FROM session_links WHERE stem = ?",
                                 (session_id,)).fetchone()
        with self._conn:
            if not is_auto:
                self._conn.execute("INSERT OR IGNORE INTO spotlights VALUES (?, '')", (name,))
            self._conn.execute("INSERT OR IGNORE INTO spotlight_pins VALUES (?, ?)",
                               (name, row["logical_session_id"] if row else session_id))

    def unpin_session(self, name: str, session_id: str) -> None:
        row = self._conn.execute("SELECT logical_session_id FROM session_links WHERE stem = ?",
                                 (session_id,)).fetchone()
        with self._conn:
            self._conn.execute("DELETE FROM spotlight_pins WHERE spotlight = ? AND session_id IN (?, ?)",
                               (name, session_id, row["logical_session_id"] if row else session_id))

    def session_choices(self) -> list[tuple[str, str, float]]:
        """(Session id, label, cost) of every top-level Session, costliest first: the pin picker."""
        infos = self._session_infos()
        return sorted(((s.session_id, infos[s.session_id].label, s.cost)
                       for s in self.session_summaries()), key=lambda x: (-x[2], x[0]))

    # Explore (ticket #11): the dimension registry and slicing live in explore.py
    def explore(self, spec: explore.ExploreSpec) -> explore.ExploreResult:
        """Cost and requests per value of `spec.group`, over the requests the filters keep."""
        return explore.run(self._conn, self._local_day, spec, self.findings)

    def explore_requests(self, spec: explore.ExploreSpec) -> list[explore.ExploreRequest]:
        """The individual requests of the slice, oldest first."""
        return explore.requests(self._conn, self._local_day, spec, self.findings)

    def explore_options(self, limit: int = 100) -> dict[str, list[str]]:
        """Most frequent values per dimension, to suggest in filter inputs."""
        return explore.options(self._conn, limit, self.findings)
