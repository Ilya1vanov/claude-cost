"""Query service: the one interface the web server and CLI use for every number."""
from __future__ import annotations

import dataclasses
import sqlite3
from dataclasses import dataclass
import calendar
from typing import Iterable, NamedTuple
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from . import cache
from . import compact as compact_mod
from . import explore
from . import findings as findings_mod
from . import pricing, session_graph, spotlights
from .findings import cold_resume as cold_resume_mod
from .localtime import day_end, day_start, local_day, today
from .projects import project_display_names
from .sql import COMPONENTS, CONTEXT_SQL, COST_SQL, SESSION_ID_SQL, SESSION_JOIN



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
    joined_as: str  # "Key match" | "Picked" | "Fork" | "Side session of <label>"
    also_in: tuple[str, ...]  # other Spotlights that count this Session fully too
    picked: bool  # a Picked session (it can be removed), also when it joined as Key match


@dataclass(frozen=True)
class SessionCandidate:
    """A Session the picker offers for a Spotlight."""
    session_id: str
    label: str
    cost: float
    kind: str | None  # "Fork" | "Side session" | None
    suggested: bool   # an unpicked Fork of a member


@dataclass(frozen=True)
class SpotlightRelated:
    """A Fork of a member that is not counted ("related, excluded")."""
    session_id: str
    label: str
    cost: float
    parent_id: str


@dataclass(frozen=True)
class SubagentDay:
    """A day's Subagent spend in one figure."""
    total: float
    runs: int         # distinct Subagent runs with spend that day
    top_type: str     # the type with the most cost (not the most runs); "unknown" when the type is missing
    all_same: bool    # every run that day has top_type


@dataclass(frozen=True)
class SpotlightDay:
    day: date
    cost: float
    sessions: dict[str, float]    # Session id -> cost that day (Subagents included)
    subagent: SubagentDay | None  # None when no Subagent spend that day
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
    kind: str  # "picked" | "auto"
    total: float  # lifetime cost; Spotlight totals overlap and must never be added together
    requests: int
    sessions: int
    first_activity: date | None
    last_activity: date | None


class _SessionAggregate(NamedTuple):
    cost: float  # lifetime
    requests: int
    first: float  # ts of the first and last Request
    last: float
    period_cost: float


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
    picked: tuple[str, ...]


@dataclass(frozen=True)
class TimelinePoint:
    message_id: str
    ts: float  # epoch seconds
    model: str
    context_tokens: int  # prompt size: input + cache read + cache writes
    cost: float
    gap_seconds: float | None  # idle time since the previous request (None for the first)
    idle: bool  # gap_seconds > the timeline_idle_gap_seconds threshold
    cache_event: str  # cold | warm | none (a Cold write is marked)
    cold_resume: bool = False  # Cold resume per its configured thresholds
    threshold: int | None = None  # Compact threshold of this request's model (None: unknown)


@dataclass(frozen=True)
class CompactionMark:
    ts: float
    trigger: str  # manual | auto
    pre_tokens: int
    post_tokens: int
    before_index: int  # number of timeline points before the compaction
    threshold: int | None = None  # Compact threshold of the model it ran on
    late: bool = False  # Late compact: came after a request above the threshold


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
    kind: str = "side"  # "side" | "fork"


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
    # Session timeline only: the Session with all its Subagents, as Top sessions counts it
    total_cost: float | None = None
    total_requests: int | None = None
    subagent_cost: float = 0.0

    @property
    def sequence(self) -> tuple[tuple[str, TimelinePoint | CompactionMark], ...]:
        """(kind, item) pairs, kind "request" or "compaction": the requests with each
        compaction placed where it happened, in time order."""
        out: list[tuple[str, TimelinePoint | CompactionMark]] = []
        pending = list(self.compactions)
        for i, p in enumerate(self.points):
            while pending and pending[0].before_index <= i:
                out.append(("compaction", pending.pop(0)))
            out.append(("request", p))
        return tuple(out + [("compaction", c) for c in pending])


PERIOD_MODES = ("last7", "week", "month")


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
    current_per_turn: float | None = None  # None when the period has no Turns
    baseline_per_turn: float | None = None


@dataclass(frozen=True)
class Usage:
    """What a Period used: Sessions with spend, Turns the user typed, Requests billed."""
    sessions: int
    turns: int | None  # None when the period starts before Turn coverage (pruned transcripts)
    requests: int


@dataclass(frozen=True)
class RankedSpend:
    key: str  # Session id or project name
    cost: float
    requests: int
    # Top sessions only: the Session's label, its Subagents (included in cost) and the Side
    # sessions / Forks started from it (not included)
    label: str = ""
    subagents: int = 0
    subagent_cost: float = 0.0
    side_sessions: int = 0
    forks: int = 0


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
    usage_current: Usage = Usage(0, 0, 0)
    usage_baseline: Usage = Usage(0, 0, 0)

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
                 key_pattern: str | None = None,
                 compare_model: str = findings_mod.DEFAULT_COMPARE_MODEL):
        self._conn = conn
        self._compare_model = compare_model
        self._thresholds = thresholds or {}
        self._key_pattern = key_pattern
        self._table = pricing.PRICE_TABLE if price_table is None else price_table
        self._tz: tzinfo | None = ZoneInfo(timezone) if timezone else None
        self._infos: dict[str, spotlights.SessionInfo] | None = None
        self._graph: session_graph.SessionGraph | None = None

    @property
    def graph(self) -> session_graph.SessionGraph:
        """The session graph, loaded on first use and kept for the life of the service."""
        if self._graph is None:
            self._graph = session_graph.load(self._conn)
        return self._graph

    def close(self) -> None:
        self._conn.close()

    def local_time(self, epoch: float) -> datetime:
        """An epoch timestamp as an aware datetime in the configured time zone."""
        return datetime.fromtimestamp(epoch, self._tz).astimezone(self._tz)

    def _local_day(self, epoch: float) -> date:
        return local_day(epoch, self._tz)

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

        Costs include the Session's Subagents. Side sessions and Forks carry their Parent
        session's id, from the Desktop store or copied history; everything else is top-level.
        """
        where, args = self._filter(surface, data_root)
        rows = self._conn.execute(
            f"SELECT {SESSION_ID_SQL} AS sid, "
            f"SUM({COST_SQL}) AS cost, COUNT(*) AS n, MIN(r.ts_epoch) AS first, "
            f"SUM(CASE WHEN a.agent_id IS NOT NULL THEN {COST_SQL} ELSE 0 END) AS sub_cost, "
            f"SUM(a.agent_id IS NOT NULL) AS sub_n "
            f"FROM requests r {SESSION_JOIN} "
            f"LEFT JOIN request_surface a ON a.message_id = r.message_id{where} "
            "GROUP BY sid ORDER BY first, sid", args).fetchall()
        graph = self.graph
        return [SessionSummary(r["sid"], r["cost"], r["n"], tuple(graph.transcripts(r["sid"])),
                               r["sub_cost"], r["sub_n"] or 0,
                               *(graph.link(r["sid"]) or (None, None))) for r in rows]

    def subagent_summaries(self, surface: str | None = None,
                           data_root: str | Path | None = None) -> list[SubagentSummary]:
        """One row per Subagent that made requests, with its own cost, oldest first."""
        where, args = self._filter(surface, data_root)
        rows = self._conn.execute(
            f"SELECT {SESSION_ID_SQL} AS sid, a.agent_id AS agent_id, "
            "s.agent_type, COALESCE(s.is_fork, 0) AS is_fork, s.tool_use_id, s.parent_agent_id, "
            f"s.description, SUM({COST_SQL}) AS cost, COUNT(*) AS n, MIN(r.ts_epoch) AS first "
            f"FROM requests r JOIN request_surface a ON a.message_id = r.message_id "
            f"{SESSION_JOIN} "
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
        """Compact threshold per model (global fallback), with cost-vs-T curve and payback.
        Read from what ingest stored; computed on the fly (and not stored) when the stored
        analysis was made with other prices or settings."""
        min_sample, step = self._threshold("compact_min_sample"), self._threshold("compact_step")
        return (compact_mod.stored(self._conn, compact_mod.key(self._table, min_sample, step))
                or compact_mod.analyze(self._conn, self._table, min_sample, step))

    def _threshold(self, name: str) -> int:
        return self._thresholds.get(name, findings_mod.all_thresholds()[name].default)

    def stale_after_seconds(self) -> int:
        """How old the last ingest may be before a page load re-ingests (config)."""
        return self._threshold("refresh_stale_seconds")

    def session_timeline(self, session_id: str, agent_id: str | None = None) -> SessionTimeline | None:
        """Timeline of a Session's own requests (its Subagents are separate, marked as launches),
        or of one Subagent with `agent_id`. None when the Session or Subagent is unknown."""
        rows = self._conn.execute(
            f"SELECT r.message_id, r.ts_epoch, r.model, {CONTEXT_SQL} AS ctx, "
            f"{COST_SQL} AS cost, COALESCE(c.event, 'none') AS event "
            f"FROM requests r {SESSION_JOIN} "
            "LEFT JOIN request_surface a ON a.message_id = r.message_id "
            "LEFT JOIN request_cache c ON c.message_id = r.message_id "
            f"WHERE {SESSION_ID_SQL} = ? AND a.agent_id IS ? "
            "ORDER BY r.ts_epoch, r.message_id", (session_id, agent_id)).fetchall()
        if not rows:
            return None
        idle_gap = self._threshold("timeline_idle_gap_seconds")
        cold_idle = self._threshold("cold_resume_idle_minutes")
        cold_ctx = self._threshold("cold_resume_min_context")
        points, prev = [], None
        for r in rows:
            gap = None if prev is None else r["ts_epoch"] - prev
            prev = r["ts_epoch"]
            points.append(TimelinePoint(
                r["message_id"], r["ts_epoch"], r["model"], r["ctx"], r["cost"], gap,
                gap is not None and gap > idle_gap, r["event"],
                cold_resume_mod.is_cold_resume(r["event"], gap, r["ctx"], cold_idle, cold_ctx)))
        analysis = self.compact_thresholds()

        def threshold(model: str) -> int | None:
            t = analysis.for_model(model)
            return t.threshold if t else None
        points = [dataclasses.replace(p, threshold=threshold(p.model)) for p in points]
        times = [p.ts for p in points]

        def index(ts: float) -> int:
            return sum(1 for t in times if t < ts)

        stems = self.graph.transcripts(session_id)
        marks = ",".join("?" * len(stems))
        compactions = tuple(self._mark(c, index(c["ts_epoch"]), points)
                            for c in self._conn.execute(
                                f"SELECT * FROM compactions WHERE stem IN ({marks}) "
                                "ORDER BY ts_epoch, uuid", stems)) if agent_id is None else ()
        launches = tuple(
            SubagentLaunch(a.agent_id, a.agent_type, a.description, a.is_fork, first, index(first),
                           a.cost, a.requests)
            for a, first in self._subagent_starts(session_id) if a.parent_agent_id == agent_id)
        sides = tuple(SideSessionMark(s.session_id, first, index(first), s.cost, s.requests,
                                      s.link_kind)
                      for s, first in self._child_sessions(session_id)) if agent_id is None else ()
        parent = next((s for s in self.session_summaries() if s.session_id == session_id), None)
        return SessionTimeline(
            session_id, agent_id, sum(p.cost for p in points), tuple(points), compactions, launches,
            sides, parent.parent_session_id if parent and agent_id is None else None,
            parent.link_kind if parent and agent_id is None else None,
            *((parent.cost, parent.requests, parent.subagent_cost)
              if parent and agent_id is None else ()))

    @staticmethod
    def _mark(c, before: int, points: list[TimelinePoint]) -> CompactionMark:
        prev = points[before - 1] if before else None  # the request just before it, and its model
        t = prev.threshold if prev else None
        return CompactionMark(c["ts_epoch"], c["trigger"], c["pre_tokens"], c["post_tokens"],
                              before, t, t is not None and prev.context_tokens > t)

    def _first_ts(self, session_id: str, agent_id: str | None = None) -> float:
        return self._conn.execute(
            f"SELECT MIN(r.ts_epoch) FROM requests r {SESSION_JOIN} "
            "LEFT JOIN request_surface a ON a.message_id = r.message_id "
            f"WHERE {SESSION_ID_SQL} = ? AND a.agent_id IS ?",
            (session_id, agent_id)).fetchone()[0]

    def _subagent_starts(self, session_id: str) -> list[tuple[SubagentSummary, float]]:
        return [(a, self._first_ts(session_id, a.agent_id))
                for a in self.subagent_summaries() if a.session_id == session_id]

    def _child_sessions(self, session_id: str) -> list[tuple[SessionSummary, float]]:
        """Sessions whose Parent session is `session_id` (Side sessions and Forks), with first timestamps."""
        children = set(self.graph.children(session_id))
        return [(s, self._first_ts(s.session_id)) for s in self.session_summaries()
                if s.session_id in children]

    def period_comparison(self, mode: str, now: datetime, include_today: bool = True,
                          top_n: int = 10) -> PeriodComparison:
        """Spend for a Period against the baseline before it, in the service's time zone.

        `include_today` only affects `last7`; calendar periods always run through today.
        """
        today = self.today(now)
        cur, base, full = resolve_periods(mode, today, include_today)
        cur_sums, base_sums = self._sums(cur), self._sums(base)
        use_cur, use_base = self._usage(cur), self._usage(base)

        def per_turn(value: float, u: Usage) -> float | None:
            return value / u.turns if u.turns else None
        components = tuple(ComponentSpend(name, cur_sums[col], base_sums[col],
                                          per_turn(cur_sums[col], use_cur),
                                          per_turn(base_sums[col], use_base))
                           for name, col in COMPONENTS)
        return PeriodComparison(
            mode, cur, base, cur.days < full and mode != "last7", cur.days, full,
            sum(cur_sums[c] for _, c in COMPONENTS), sum(base_sums[c] for _, c in COMPONENTS),
            components, self._top_sessions(cur, top_n),
            self._top("r.project", cur, top_n), use_cur, use_base)

    def _usage(self, p: Period) -> Usage:
        lo, hi = self._bounds(p)
        row = self._conn.execute(
            f"SELECT COUNT(DISTINCT {SESSION_ID_SQL}) AS s, COUNT(*) AS n "
            f"FROM requests r {SESSION_JOIN} WHERE r.ts_epoch >= ? AND r.ts_epoch < ?",
            (lo, hi)).fetchone()
        cov = self._conn.execute("SELECT value FROM meta WHERE key = 'turns_from'").fetchone()
        turns = None if cov and lo < float(cov["value"]) else self._conn.execute(
            "SELECT COUNT(*) FROM turns WHERE ts_epoch >= ? AND ts_epoch < ?", (lo, hi)).fetchone()[0]
        return Usage(row["s"], turns, row["n"])

    def project_names(self) -> dict[str, str]:
        """Project key -> display name, used wherever a Project is shown."""
        return project_display_names(self._conn)

    def _bounds(self, p: Period) -> tuple[float, float]:
        """Epoch range [start, end) covering the Period's local days."""
        return day_start(p.start, self._tz), day_end(p.end, self._tz)

    def _sums(self, p: Period) -> dict[str, float]:
        lo, hi = self._bounds(p)
        cols = ", ".join(f"COALESCE(SUM({c}), 0) AS {c}" for _, c in COMPONENTS)
        row = self._conn.execute(
            f"SELECT {cols} FROM requests WHERE ts_epoch >= ? AND ts_epoch < ?", (lo, hi)).fetchone()
        return {c: row[c] for _, c in COMPONENTS}

    def session_spend(self, session_id: str, p: Period) -> tuple[float, int]:
        """A Session's cost and requests in the Period, Subagents included, as Top sessions counts them."""
        lo, hi = self._bounds(p)
        row = self._conn.execute(
            f"SELECT COALESCE(SUM({COST_SQL}), 0) AS cost, COUNT(*) AS n FROM requests r {SESSION_JOIN} "
            f"WHERE {SESSION_ID_SQL} = ? AND r.ts_epoch >= ? AND r.ts_epoch < ?",
            (session_id, lo, hi)).fetchone()
        return row["cost"], row["n"]

    def _top(self, key_sql: str, p: Period, n: int) -> tuple[RankedSpend, ...]:
        lo, hi = self._bounds(p)
        rows = self._conn.execute(
            f"SELECT {key_sql} AS k, SUM({COST_SQL}) AS cost, COUNT(*) AS n "
            f"FROM requests r {SESSION_JOIN} "
            "WHERE r.ts_epoch >= ? AND r.ts_epoch < ? GROUP BY k ORDER BY cost DESC, k LIMIT ?",
            (lo, hi, n)).fetchall()
        return tuple(RankedSpend(r["k"], r["cost"], r["n"]) for r in rows)

    def _top_sessions(self, p: Period, n: int) -> tuple[RankedSpend, ...]:
        """Top sessions with label, Subagent count/cost and Side session/Fork counts.
        Cost and requests are unchanged: Subagents included, Side sessions and Forks not."""
        ranked = self._top(SESSION_ID_SQL, p, n)
        lo, hi = self._bounds(p)
        subs = {r["k"]: (r["n"], r["cost"]) for r in self._conn.execute(
            f"SELECT {SESSION_ID_SQL} AS k, COUNT(DISTINCT a.agent_id) AS n, "
            f"SUM({COST_SQL}) AS cost FROM requests r {SESSION_JOIN} "
            "JOIN request_surface a ON a.message_id = r.message_id AND a.agent_id IS NOT NULL "
            "WHERE r.ts_epoch >= ? AND r.ts_epoch < ? GROUP BY k", (lo, hi))}
        infos = self._session_infos()
        top = {r.key for r in ranked}
        children = [c for t in top for c in self.graph.children(t)]
        firsts = {r["k"]: r["t"] for r in self._conn.execute(
            f"SELECT {SESSION_ID_SQL} AS k, MIN(r.ts_epoch) AS t FROM requests r {SESSION_JOIN} "
            "LEFT JOIN request_surface a ON a.message_id = r.message_id "
            f"WHERE a.agent_id IS NULL AND {SESSION_ID_SQL} IN ({','.join('?' * len(children))}) "
            "GROUP BY k", children)} if children else {}
        linked: dict[tuple[str, str], int] = {}
        for sid in children:
            first = firsts.get(sid)  # None when every request is Subagent-owned
            if first is not None and lo <= first < hi:  # started in the period
                key = self.graph.link(sid)
                linked[key] = linked.get(key, 0) + 1
        labels = {sid: i.label for sid, i in infos.items()}
        return tuple(RankedSpend(
            r.key, r.cost, r.requests, labels.get(r.key, r.key),
            *subs.get(r.key, (0, 0.0)),
            linked.get((r.key, "side"), 0), linked.get((r.key, "fork"), 0)) for r in ranked)

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
        today = self.today(now)
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
        lo = day_start(since, self._tz) if since else None
        hi = day_end(until, self._tz) if until else None
        rows = findings_mod.load_rows(self._conn, self._tz, lo, hi)
        by_id = {r.message_id: r for r in rows}
        defs = findings_mod.all_findings()
        ctx = findings_mod.FindingContext(rows, self._thresholds, findings_mod.all_thresholds(),
                                          self._table, self._conn,
                                          self.compact_thresholds(), self._compare_model,
                                          median_first_context=cache.median_first_context(self._conn),
                                          graph=self.graph)
        return [findings_mod.build_finding(d, d.detect(ctx), by_id) for d in defs]

    def top_findings(self, mode: str, now: datetime, include_today: bool = True,
                     top_n: int = 10) -> list[findings_mod.Finding]:
        """The findings covering the most spend inside the Period (same periods as
        period_comparison), costliest first; findings that cover nothing are left out."""
        cur, _, _ = resolve_periods(mode, self.today(now), include_today)
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
        today = self.today(now)
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
        today = self.today(now)
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
        # A partial period is judged on the days that have elapsed, so every period is
        # compared over the same number of days (the full lists stay for the chart).
        elapsed = [len(d) for p, d in zip(periods, dailies) if p.end > today]
        if elapsed:
            n = min(elapsed)
            costs = [sum(d[:n]) for d in dailies]
        base_cost = costs[starts.index(baseline)]
        entries = tuple(
            ComparisonEntry(p, c, d, p.end > today, c - base_cost,
                            (c - base_cost) / base_cost * 100 if base_cost else None)
            for p, c, d in zip(periods, costs, dailies))
        return Comparison(mode, baseline, entries, max(p.days for p in periods))

    def today(self, now: datetime) -> date:
        """`now` as a calendar day in the configured time zone."""
        return today(now, self._tz)

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
        """Every logical Session with the fields Spotlights search and its Side/Fork link.
        Computed once per service (a service lives for one request)."""
        if self._infos is None:
            self._infos = self._build_session_infos()
        return self._infos

    def _build_session_infos(self) -> dict[str, spotlights.SessionInfo]:
        summaries = self.session_summaries()
        meta = {r["stem"]: r for r in self._conn.execute("SELECT * FROM session_meta")}
        out = {}
        for s in summaries:
            rows = [meta[t] for t in s.transcripts if t in meta]

            def vals(col: str, rows=rows) -> tuple[str, ...]:
                return tuple(r[col] for r in rows if r[col])
            out[s.session_id] = spotlights.SessionInfo(
                s.session_id, vals("custom_title"), vals("git_branch"), vals("cwd"),
                vals("first_prompt"), s.parent_session_id, s.link_kind)
        return out

    def auto_spotlights_enabled(self) -> bool:
        return bool(self._key_pattern)

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
        picked: dict[str, set[str]] = {}
        for r in self._conn.execute("SELECT spotlight, session_id FROM spotlight_pins"):
            picked.setdefault(r["spotlight"], set()).add(self.graph.session_of(r["session_id"]))
        defs = [spotlights.Definition(r["name"], "picked", "", frozenset(picked.get(r["name"], ())))
                for r in self._conn.execute("SELECT name FROM spotlights ORDER BY name")]
        taken = {d.name for d in defs}
        found = sorted({k for ks in keys.values() for k in ks} - taken)
        return defs + [spotlights.Definition(k, "auto", k, frozenset(picked.get(k, ()))) for k in found]

    def _spotlight_state(self):
        infos = self._session_infos()
        pat = self._key_regex()
        keys = {sid: spotlights.detect_keys(info, pat) for sid, info in infos.items()}
        defs = self._definitions(infos, keys)
        return infos, defs, {d.name: spotlights.resolve(d, infos, keys) for d in defs}

    def _request_rows(self) -> list:
        return self._conn.execute(
            f"SELECT {SESSION_ID_SQL} AS sid, a.agent_id AS agent, "
            f"r.ts_epoch AS ts, {COST_SQL} AS cost, "
            + ", ".join(f"r.{c}" for _, c in COMPONENTS) + " "
            f"FROM requests r {SESSION_JOIN} "
            "LEFT JOIN request_surface a ON a.message_id = r.message_id").fetchall()

    def _spotlight_summary(self, defn, total: float, requests: int, sessions: int,
                           first_ts: float | None, last_ts: float | None) -> SpotlightSummary:
        return SpotlightSummary(
            defn.name, defn.kind, total, requests, sessions,
            self._local_day(first_ts) if first_ts is not None else None,
            self._local_day(last_ts) if last_ts is not None else None)

    def _summary(self, defn, members: frozenset[str], rows: list) -> SpotlightSummary:
        mine = [r for r in rows if r["sid"] in members]
        times = [r["ts"] for r in mine]
        return self._spotlight_summary(
            defn, sum(r["cost"] for r in mine), len(mine), len(members),
            min(times) if times else None, max(times) if times else None)

    def spotlights(self) -> list[SpotlightSummary]:
        """Every Spotlight with its lifetime total and first and last activity, costliest first.

        Totals overlap when a Session is in several Spotlights: never add them together.
        """
        _, defs, member = self._spotlight_state()
        rows = self._request_rows()
        out = [self._summary(d, member[d.name].members, rows) for d in defs]
        return sorted(out, key=lambda s: (-s.total, s.name))

    def _session_aggregates(self, lo: float, hi: float) -> dict[str, _SessionAggregate]:
        """Per Session: lifetime cost, Request count, first and last ts, cost inside [lo, hi)."""
        return {r["sid"]: _SessionAggregate(r["cost"], r["n"], r["first"], r["last"], r["period_cost"])
                for r in self._conn.execute(
                    f"SELECT {SESSION_ID_SQL} AS sid, SUM({COST_SQL}) AS cost, COUNT(*) AS n, "
                    "MIN(r.ts_epoch) AS first, MAX(r.ts_epoch) AS last, "
                    f"SUM(CASE WHEN r.ts_epoch >= ? AND r.ts_epoch < ? THEN {COST_SQL} ELSE 0 END) "
                    "AS period_cost "
                    f"FROM requests r {SESSION_JOIN} "
                    "GROUP BY sid", (lo, hi))}

    def top_spotlights(self, mode: str, now: datetime, include_today: bool = True,
                       top_n: int = 10) -> list[SpotlightRank]:
        """Spotlights ranked by spend inside the Period (same periods as period_comparison)."""
        cur, _, _ = resolve_periods(mode, self.today(now), include_today)
        lo, hi = self._bounds(cur)
        _, defs, member = self._spotlight_state()
        agg = self._session_aggregates(lo, hi)
        ranked = []
        for d in defs:
            members = member[d.name].members
            mine = [agg[sid] for sid in members if sid in agg]
            period_cost = sum(a.period_cost for a in mine)
            if period_cost > 0:
                ranked.append(SpotlightRank(self._spotlight_summary(
                    d, sum(a.cost for a in mine), sum(a.requests for a in mine), len(members),
                    min(a.first for a in mine), max(a.last for a in mine)), period_cost))
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
                              by[sid].requests, self._joined_as(sid, infos, ms, defn),
                              tuple(sorted(in_other.get(sid, ()))), sid in defn.picked)
             for sid in ms.members if sid in by), key=lambda x: (-x.cost, x.session_id)))
        related = tuple(sorted(
            (SpotlightRelated(sid, infos[sid].label, by[sid].cost if sid in by else 0.0,
                              infos[sid].parent or "") for sid in ms.related),
            key=lambda x: (-x.cost, x.session_id)))
        subs = tuple(SpotlightSubagent(a.session_id, a.agent_id, a.agent_type, a.description, a.cost)
                     for a in self.subagent_summaries() if a.session_id in ms.members)
        agent_types = {(a.session_id, a.agent_id): a.agent_type or "unknown"
                       for a in self.subagent_summaries()}
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
                key = (r["sid"], r["agent"])
                d[2][key] = d[2].get(key, 0.0) + total
        def day_subagent(runs: dict) -> SubagentDay | None:
            if not runs:
                return None
            by_type: dict[str, float] = {}
            for key, cost in runs.items():
                t = agent_types.get(key, "unknown")
                by_type[t] = by_type.get(t, 0.0) + cost
            top = max(by_type, key=lambda t: (by_type[t], t))
            return SubagentDay(sum(runs.values()), len(runs), top, len(by_type) == 1)

        out_days = ()
        if days:
            first, last = min(days), max(days)
            blank = [0.0, {}, {}, {n: 0.0 for n, _ in COMPONENTS}]
            out_days = tuple(
                SpotlightDay(day, (c := days.get(day, blank))[0], c[1], day_subagent(c[2]), c[3])
                for day in (first + timedelta(n) for n in range((last - first).days + 1)))
        return SpotlightDetail(summary, sessions, subs, related, out_days, comps,
                               tuple(sorted(defn.picked)))

    def spotlight_comparison(self, names: list[str]) -> list[SpotlightDetail]:
        """Two or more Spotlights side by side, each in full (never summed). KeyError if unknown."""
        names = list(dict.fromkeys(names))
        if len(names) < 2:
            raise ValueError("select at least two different Spotlights")
        return [self.spotlight(n) for n in names]

    @staticmethod
    def _joined_as(sid: str, infos: dict[str, spotlights.SessionInfo],
                   ms: spotlights.Membership, defn: spotlights.Definition) -> str:
        """How a member Session joined: Key match beats Picked; a Fork is only in when picked."""
        info = infos[sid]
        if sid in ms.key_matched:
            return "Key match"
        if info.kind == "fork":
            return "Fork"
        if sid in defn.picked:
            return "Picked"
        parent = infos[info.parent].label if info.parent in infos else ""
        return f"Side session of {parent}"

    # Editing user-defined Spotlights (Picked sessions live in the store, not config.toml).

    def create_spotlight(self, name: str, session_ids: Iterable[str] = ()) -> None:
        """Create a Spotlight (empty with just a name) and add Picked sessions to it."""
        name = name.strip()
        if not name:
            raise spotlights.SpotlightError("a Spotlight needs a name")
        with self._conn:
            self._conn.execute("INSERT OR IGNORE INTO spotlights (name) VALUES (?)", (name,))
            self._conn.executemany("INSERT OR IGNORE INTO spotlight_pins VALUES (?, ?)",
                                   [(name, self.graph.session_of(s)) for s in session_ids])

    def delete_spotlight(self, name: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM spotlights WHERE name = ?", (name,))
            self._conn.execute("DELETE FROM spotlight_pins WHERE spotlight = ?", (name,))

    def add_session(self, name: str, session_id: str) -> None:
        """Add a Picked session to a Spotlight: an Auto-spotlight of that name, else a
        user-defined one (created if it does not exist yet)."""
        name = name.strip()
        if not name:
            raise spotlights.SpotlightError("a Spotlight needs a name")
        is_auto = any(name in ks for ks in self.session_keys().values())
        with self._conn:
            if not is_auto:
                self._conn.execute("INSERT OR IGNORE INTO spotlights (name) VALUES (?)", (name,))
            self._conn.execute("INSERT OR IGNORE INTO spotlight_pins VALUES (?, ?)",
                               (name, self.graph.session_of(session_id)))

    def remove_session(self, name: str, session_id: str) -> None:
        """Remove a Picked session; Side sessions that joined through it leave with it.
        A Key-match Session of an Auto-spotlight cannot be removed."""
        sid = self.graph.session_of(session_id)
        infos, defs, member = self._spotlight_state()
        if any(d.name == name and sid in member[name].key_matched for d in defs):
            raise spotlights.SpotlightError("a Key-match Session cannot be removed")
        with self._conn:
            self._conn.execute("DELETE FROM spotlight_pins WHERE spotlight = ? AND session_id IN (?, ?)",
                               (name, session_id, sid))

    def session_labels(self) -> dict[str, str]:
        """Label of every logical Session id: custom title, else the first 60 characters of the
        first prompt, else the id. The single label source every page (and Top sessions) uses."""
        return {sid: info.label for sid, info in self._session_infos().items()}

    SEARCH_LIMIT = 10

    def search_sessions(self, query: str, name: str,
                        exclude: Iterable[str] = ()) -> list[SessionCandidate]:
        """Sessions to pick for the Spotlight `name` (a name with no Spotlight yet has no members).

        Candidates are every Session except the Spotlight's direct members and `exclude`. An empty
        query returns the suggested Forks (unpicked Forks of members), costliest first. Otherwise
        the query is matched as a case-insensitive subsequence of the label or the id; suggested
        Forks rank first, then the better match, then the costlier Session; at most 10 are returned.
        """
        infos, defs, member = self._spotlight_state()
        defn = next((d for d in defs if d.name == name), None)
        ms = member.get(name)
        direct = (ms.key_matched | defn.picked) if ms and defn else frozenset()
        skip = direct | {self.graph.session_of(s) for s in exclude}
        suggested = ms.related if ms else frozenset()
        query = query.strip()
        ranked = []
        for s in self.session_summaries():
            sid = s.session_id
            if sid in skip:
                continue
            info = infos[sid]
            if query:
                scores = [m for m in (spotlights.match_score(query, info.label),
                                      spotlights.match_score(query, sid)) if m is not None]
                if not scores:
                    continue
                score = max(scores)
            elif sid in suggested:
                score = 0
            else:
                continue
            kind = {"fork": "Fork", "side": "Side session"}.get(info.kind or "")
            ranked.append((sid not in suggested, -score, -s.cost, sid,
                           SessionCandidate(sid, info.label, s.cost, kind, sid in suggested)))
        ranked.sort(key=lambda r: r[:4])
        return [r[4] for r in ranked[:self.SEARCH_LIMIT]]

    # Explore (ticket #11): the dimension registry and slicing live in explore.py
    def explore(self, spec: explore.ExploreSpec) -> explore.ExploreResult:
        """Cost and requests per value of `spec.group`, over the requests the filters keep."""
        return explore.run(self._conn, self._local_day, spec, self.findings)

    def explore_requests(self, spec: explore.ExploreSpec) -> list[explore.ExploreRequest]:
        """The individual requests of the slice, oldest first."""
        return explore.requests(self._conn, self._local_day, spec, self.findings)

    def explore_options(self, limit: int = 100) -> dict[str, list[str]]:
        """Most frequent values per dimension, to suggest in filter inputs."""
        return explore.options(self._conn, limit)
