"""Query service: the one interface the web server and CLI use for every number."""
from __future__ import annotations

import dataclasses
import sqlite3
import uuid
from dataclasses import dataclass, field
import calendar
from typing import Iterable, NamedTuple
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from . import cache
from . import compact as compact_mod
from . import explore
from . import findings as findings_mod
from . import pricing, session_graph, spotlights, store
from . import timeline_rows as timeline_rows_mod
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


@dataclass
class _DayAcc:
    """One Spotlight day while it is being summed."""
    cost: float = 0.0
    by_session: dict = field(default_factory=dict)
    by_run: dict = field(default_factory=dict)  # (session, agent) -> cost
    components: dict = field(default_factory=lambda: {n: 0.0 for n, _ in COMPONENTS})


@dataclass(frozen=True)
class SpotlightSession:
    session_id: str
    label: str
    cost: float  # includes the Session's Subagents
    subagent_cost: float
    requests: int
    joined_as: str  # "Key match" | "Picked" | "Fork" | "Side session of <label>"
    also_in: tuple[str, ...]  # other Spotlights that count this Session fully too
    picked: bool  # a Picked session, also when it joined as Key match
    key_matched: bool  # an Auto-spotlight's Key match: shown as a fixed chip
    removable: bool  # a direct member (picked or Key match): the Remove control applies
    via: str | None = None  # the included Spotlight that brought it in, when it did
    parent_id: str | None = None  # its parent Session when that is in the Spotlight, else None (a root)
    depth: int = 0  # nesting level in the Session tree
    subtree_cost: float = 0.0  # own cost plus every descendant's
    also_in_ids: tuple[str, ...] = ()  # ids of the Spotlights in also_in, same order


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
    includes: int = 0  # how many Spotlights it includes
    id: str = ""  # stable identity (ADR-0004); the name is an editable label
    key: str = ""  # an Auto-spotlight's key, '' for a user-defined Spotlight
    locked: bool = False  # a Locked auto-spotlight: sessions carrying its key are only suggested


@dataclass(frozen=True)
class SpotlightIncluded:
    """An Included spotlight as its includer's page lists it."""
    name: str
    kind: str  # "picked" | "auto"
    cost: float  # that Spotlight's own total
    sessions: int
    id: str = ""


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
class SpotlightDismissed:
    """A Dismissed session of an Auto-spotlight, listed so it can be restored."""
    session_id: str
    label: str
    cost: float


@dataclass(frozen=True)
class SpotlightSuggested:
    """A Suggested session of an Auto-spotlight: where it carries the key, not counted until accepted."""
    session_id: str
    label: str
    cost: float
    field: str    # "title" | "branch" | "worktree" | "prompt"
    snippet: str


@dataclass(frozen=True)
class SuggestionGroup:
    """Pending suggestions for one key; `spotlight_id` is None while the key has no Auto-spotlight."""
    key: str
    spotlight_id: str | None
    name: str
    suggested: tuple[SpotlightSuggested, ...]


@dataclass(frozen=True)
class SessionSuggestion:
    """A key a Session is suggested for, and where the Session carries it."""
    key: str
    spotlight_id: str | None
    name: str
    field: str
    snippet: str


@dataclass(frozen=True)
class SpotlightDetail:
    summary: SpotlightSummary
    sessions: tuple[SpotlightSession, ...]
    subagents: tuple[SpotlightSubagent, ...]
    related: tuple[SpotlightRelated, ...]
    days: tuple[SpotlightDay, ...]  # lifetime, first to last activity, days without spend included
    components: dict[str, float]  # lifetime cost per Cost component
    picked: tuple[str, ...]
    included: tuple[SpotlightIncluded, ...] = ()
    dismissed: tuple[SpotlightDismissed, ...] = ()
    suggested: tuple[SpotlightSuggested, ...] = ()


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
    effort: str = ""  # Effort the request was made with ("" when not recorded)


@dataclass(frozen=True)
class ModelEffort:
    """One model + Effort combination a timeline's requests used."""
    model: str
    effort: str  # "" when not recorded
    requests: int
    cost: float


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
class SettingChange:
    """A request whose model or Effort differs from the previous request's in its Stream."""
    before_index: int  # index of the request it happens before
    ts: float  # that request's time
    from_model: str
    from_effort: str  # "" when not recorded
    to_model: str
    to_effort: str


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
    setting_changes: tuple[SettingChange, ...] = ()
    # The Timeline: rows (tagged by `kind`) in time order. Read from the transcripts, so only
    # filled when asked for (`session_timeline(with_rows=True)`).
    rows: tuple[timeline_rows_mod.Row, ...] = ()

    @property
    def combinations(self) -> tuple[ModelEffort, ...]:
        """The model + Effort combinations of this timeline's own requests, in order of first use."""
        requests: dict[tuple[str, str], int] = {}
        costs: dict[tuple[str, str], float] = {}
        for p in self.points:
            key = (p.model, p.effort)
            requests[key] = requests.get(key, 0) + 1
            costs[key] = costs.get(key, 0.0) + p.cost
        return tuple(ModelEffort(m, e, requests[m, e], costs[m, e]) for m, e in requests)

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
CUSTOM_MODE = "custom"


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
    """What a Period used: Sessions with spend, Turns the user typed, Requests billed and the
    Subagent runs among them."""
    sessions: int
    turns: int | None  # None when the period starts before Turn coverage (pruned transcripts)
    requests: int
    subagents: int = 0  # distinct Subagent runs with requests, nested ones included


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
    cache_loss: float = 0.0  # $ Cold writes cost beyond reading the same tokens
    cache_loss_share: float | None = None  # cache_loss / the Period's total Cost (None if no Cost)


@dataclass(frozen=True)
class CacheCard:
    mode: str
    current: CacheStats
    baseline: CacheStats

    @property
    def delta_hit_ratio(self) -> float | None:
        a, b = self.current.hit_ratio, self.baseline.hit_ratio
        return None if a is None or b is None else a - b

    @property
    def delta_cache_loss_share(self) -> float | None:
        a, b = self.current.cache_loss_share, self.baseline.cache_loss_share
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


def resolve_periods(mode: str, today: date, include_today: bool = True,
                    custom: Period | None = None) -> tuple[Period, Period, int]:
    """(current, baseline, full period length). Partial calendar periods compare on equal elapsed days.

    `custom` (required for mode "custom") is a run of whole local days: its Baseline is the run of
    the same length just before it, and it is never partial."""
    if mode == CUSTOM_MODE:
        if custom is None:
            raise ValueError("custom period needs start and end dates")
        if custom.end < custom.start:
            raise ValueError(f"custom period ends ({custom.end}) before it starts ({custom.start})")
        if custom.start > today:
            raise ValueError(f"custom period starts ({custom.start}) after today ({today})")
        if custom.end > today:
            raise ValueError(f"custom period ends ({custom.end}) after today ({today})")
        n = custom.days
        return custom, Period(custom.start - timedelta(n), custom.start - timedelta(1)), n
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
        self._hits: dict[str, dict[str, list[spotlights.KeyHit]]] | None = None
        self._carriers: dict[str, list[tuple[str, spotlights.KeyHit]]] | None = None
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

    def session_timeline(self, session_id: str, agent_id: str | None = None,
                         with_rows: bool = False) -> SessionTimeline | None:
        """Timeline of a Session's own requests (its Subagents are separate, marked as launches),
        or of one Subagent with `agent_id`. None when the Session or Subagent is unknown.
        `with_rows` also reads the transcripts for the Timeline rows (ADR 0005)."""
        rows = self._conn.execute(
            f"SELECT r.message_id, r.ts_epoch, r.model, {CONTEXT_SQL} AS ctx, "
            f"{COST_SQL} AS cost, COALESCE(c.event, 'none') AS event, "
            "COALESCE(d.effort, '') AS effort "
            f"FROM requests r {SESSION_JOIN} "
            "LEFT JOIN request_dims d ON d.message_id = r.message_id "
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
                cold_resume_mod.is_cold_resume(r["event"], gap, r["ctx"], cold_idle, cold_ctx),
                effort=r["effort"]))
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
        timeline = SessionTimeline(
            session_id, agent_id, sum(p.cost for p in points), tuple(points), compactions, launches,
            sides, parent.parent_session_id if parent and agent_id is None else None,
            parent.link_kind if parent and agent_id is None else None,
            *((parent.cost, parent.requests, parent.subagent_cost)
              if parent and agent_id is None else ()),
            setting_changes=self._setting_changes(points))
        if not with_rows:
            return timeline
        main_paths = [] if agent_id else self._main_paths(stems)
        content, prompts = timeline_rows_mod.read_content(
            self._copies(self._tree_message_ids(timeline)), main_paths)
        return dataclasses.replace(
            timeline, rows=self._timeline_rows(timeline, stems, main_paths, content, prompts))

    def _main_paths(self, stems: list[str]) -> list[str]:
        """The Session's own transcripts (not its Subagents')."""
        return [r["path"] for r in self._conn.execute(
            f"SELECT path FROM ingest_files WHERE file_stem IN ({','.join('?' * len(stems))}) "
            "AND path NOT IN (SELECT path FROM subagent_files) ORDER BY path", stems)]

    def _copies(self, ids: list[str]) -> dict[str, list[str]]:
        """Message id -> the transcript paths holding it, best copy first."""
        copies: dict[str, list[str]] = {}
        for i in range(0, len(ids), 500):  # stay under SQLite's variable limit
            chunk = ids[i:i + 500]
            for c in self._conn.execute(
                    "SELECT message_id, path FROM request_copies "
                    f"WHERE message_id IN ({','.join('?' * len(chunk))}) "
                    "ORDER BY is_copy, path", chunk):
                copies.setdefault(c["message_id"], []).append(c["path"])
        return copies

    def _tree_message_ids(self, t: SessionTimeline) -> list[str]:
        """The message ids of `t`'s requests and of every Subagent under it, so one Timeline
        request reads each transcript once (ADR 0005)."""
        parent = {a.agent_id: a.parent_agent_id for a, _ in self._subagent_starts(t.session_id)}

        def under(agent: str | None) -> bool:
            while agent is not None and agent != t.agent_id:
                agent = parent.get(agent)
            return t.agent_id is None or agent == t.agent_id
        return [r["message_id"] for r in self._conn.execute(
            f"SELECT r.message_id, a.agent_id FROM requests r {SESSION_JOIN} "
            "LEFT JOIN request_surface a ON a.message_id = r.message_id "
            f"WHERE {SESSION_ID_SQL} = ?", (t.session_id,)) if under(r["agent_id"])]

    def _timeline_rows(self, t: SessionTimeline, stems: list[str], main_paths: list[str],
                       content: dict[str, timeline_rows_mod.Content],
                       prompts: list[timeline_rows_mod.PromptRow]) -> tuple:
        """Prompt and request rows: each request with its content, each prompt before the
        requests it answers. Prompts are the Session's own; a Subagent's stream has none."""
        requests = [
            timeline_rows_mod.RequestRow(
                p.ts, p.message_id, p.model, p.effort,
                content[p.message_id].text if p.message_id in content else "",
                content[p.message_id].tools if p.message_id in content else (),
                p.context_tokens, p.cost, p.gap_seconds, p.idle,
                p.cache_event == cache.COLD, p.cold_resume,
                p.threshold is not None and p.context_tokens > p.threshold, p.threshold,
                p.message_id in content)
            for p in t.points]
        events = self._event_rows(t, stems, main_paths)
        subagents = self._subagent_rows(t, stems, content)
        return tuple(sorted([*prompts, *requests, *events, *subagents],
                            key=timeline_rows_mod.row_order))

    def _event_rows(self, t: SessionTimeline, stems: list[str], main_paths: list[str]) -> list:
        """Event rows from the timeline's marks. The Fork's opening row is the Session's own."""
        out: list = [timeline_rows_mod.SettingChangeRow(
            c.ts, c.from_model, c.from_effort, c.to_model, c.to_effort) for c in t.setting_changes]
        if t.compactions:  # the same rows, in the same order, as the compactions' query
            uuids = [r["uuid"] for r in self._conn.execute(
                f"SELECT uuid FROM compactions WHERE stem IN ({','.join('?' * len(stems))}) "
                "ORDER BY ts_epoch, uuid", stems)]
            summaries = timeline_rows_mod.read_summaries(main_paths, set(uuids))
            out += [timeline_rows_mod.CompactionRow(
                c.ts, c.trigger, c.pre_tokens, c.post_tokens, c.late, summaries.get(u, ""))
                for c, u in zip(t.compactions, uuids)]
        out += [timeline_rows_mod.IdleGapRow(prev.ts, p.gap_seconds)
                for prev, p in zip(t.points, t.points[1:]) if p.idle]
        labels = self.session_labels() if t.side_sessions or t.parent_session_id else {}
        out += [timeline_rows_mod.ChildSessionRow(
            self._fork_point(s.session_id, s.ts) if s.kind == "fork" else s.ts, s.session_id,
            labels.get(s.session_id, s.session_id), s.kind, s.requests, s.cost)
            for s in t.side_sessions]
        if t.link_kind == "fork" and t.points:
            out.append(timeline_rows_mod.ForkedFromRow(
                self._fork_point(t.session_id, t.points[0].ts), t.parent_session_id,
                labels.get(t.parent_session_id, t.parent_session_id)))
        return out

    def _fork_point(self, session_id: str, first_own: float) -> float:
        """Where a Fork left its Parent session: the last request of the copied history in its
        transcripts; `first_own` (its first own request) when it holds no copies (a Desktop link)."""
        stems = self.graph.transcripts(session_id)
        last = self._conn.execute(
            "SELECT MAX(c.ts_epoch) FROM request_copies c JOIN ingest_files f ON f.path = c.path "
            f"WHERE c.is_copy = 1 AND f.file_stem IN ({','.join('?' * len(stems))}) "
            "AND c.path NOT IN (SELECT path FROM subagent_files)", stems).fetchone()[0]
        return first_own if last is None else last

    def _subagent_rows(self, t: SessionTimeline, stems: list[str],
                       content: dict[str, timeline_rows_mod.Content]
                       ) -> list[timeline_rows_mod.SubagentRow]:
        """One block per direct child launch, holding the child's own rows (so nested Subagents
        resolve through the parent agent link, recursively), from the content already read."""
        blocks = []
        for a in t.subagents:
            child = self.session_timeline(t.session_id, a.agent_id)
            if child is None or not child.points:
                continue
            first = child.points[0]
            blocks.append(timeline_rows_mod.SubagentRow(
                a.ts, a.agent_id, a.agent_type, a.description, a.is_fork, first.model, first.effort,
                a.requests, max(p.context_tokens for p in child.points), a.cost,
                self._timeline_rows(child, stems, [], content, [])))
        return blocks

    @staticmethod
    def _setting_changes(points: list[TimelinePoint]) -> tuple[SettingChange, ...]:
        """Model changes, and Effort changes between two recorded Efforts; both at once is one."""
        return tuple(
            SettingChange(i, b.ts, a.model, a.effort, b.model, b.effort)
            for i, (a, b) in enumerate(zip(points, points[1:]), 1)
            if a.model != b.model or (a.effort and b.effort and a.effort != b.effort))

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
                          top_n: int = 10, custom: Period | None = None) -> PeriodComparison:
        """Spend for a Period against the baseline before it, in the service's time zone.

        `include_today` only affects `last7`; calendar periods always run through today.
        """
        today = self.today(now)
        cur, base, full = resolve_periods(mode, today, include_today, custom)
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
            f"SELECT COUNT(DISTINCT {SESSION_ID_SQL}) AS s, COUNT(*) AS n, "
            "COUNT(DISTINCT CASE WHEN a.agent_id IS NOT NULL THEN r.session_id || '/' || a.agent_id END) AS sub "
            f"FROM requests r LEFT JOIN request_surface a ON a.message_id = r.message_id {SESSION_JOIN} WHERE r.ts_epoch >= ? AND r.ts_epoch < ?",
            (lo, hi)).fetchone()
        cov = self._conn.execute("SELECT value FROM meta WHERE key = 'turns_from'").fetchone()
        turns = None if cov and lo < float(cov["value"]) else self._conn.execute(
            "SELECT COUNT(*) FROM turns WHERE ts_epoch >= ? AND ts_epoch < ?", (lo, hi)).fetchone()[0]
        return Usage(row["s"], turns, row["n"], row["sub"])

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
        loss = self._cache_loss(lo, hi)
        spend = sum(self._sums(p).values())
        return CacheStats(row["rd"] / total if total else None, row["cold"], row["warm"],
                          row["w5"], row["w1"], row["rebuilds"], loss,
                          loss / spend if spend else None)

    def _cache_loss(self, lo: float, hi: float) -> float:
        """Each Cold write's cost less the same tokens at that request's own read rate."""
        return sum(r.write_cost - (r.write_5m + r.write_1h) * pricing.read_rate(r, self._table)
                   for r in findings_mod.load_rows(self._conn, self._tz, lo, hi)
                   if r.cache_event == cache.COLD)

    def cache_card(self, mode: str, now: datetime, include_today: bool = True,
                   custom: Period | None = None) -> CacheCard:
        """Cache metrics for the Period against its baseline (same periods as period_comparison)."""
        today = self.today(now)
        cur, base, _ = resolve_periods(mode, today, include_today, custom)
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
                                          graph=self.graph, period_start=lo, period_end=hi)
        return [findings_mod.build_finding(d, d.detect(ctx), by_id) for d in defs]

    def top_findings(self, mode: str, now: datetime, include_today: bool = True,
                     top_n: int = 10, custom: Period | None = None) -> list[findings_mod.Finding]:
        """The findings covering the most spend inside the Period (same periods as
        period_comparison), costliest first; findings that cover nothing are left out."""
        cur, _, _ = resolve_periods(mode, self.today(now), include_today, custom)
        hits = [f for f in self.findings(cur.start, cur.end) if f.attributed > 0]
        return sorted(hits, key=lambda f: (-f.attributed, f.id))[:top_n]

    def finding(self, finding_id: str, since: date | None = None) -> findings_mod.Finding:
        for f in self.findings(since):
            if f.id == finding_id:
                return f
        raise KeyError(finding_id)

    def first_request_day(self) -> date | None:
        """The local day of the earliest Request; None for an empty store."""
        row = self._conn.execute("SELECT MIN(ts_epoch) AS first FROM requests").fetchone()
        return None if row["first"] is None else self._local_day(row["first"])

    def available_periods(self, mode: str, now: datetime) -> list[date]:
        """Start dates of every calendar period from the first request to now, newest first."""
        if mode not in COMPARISON_MODES:
            raise ValueError(f"mode must be one of {COMPARISON_MODES}")
        first = self.first_request_day()
        if first is None:
            return []
        today = self.today(now)
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

    def _key_hits(self) -> dict[str, dict[str, list[spotlights.KeyHit]]]:
        """Per Session, every key the pattern finds with where it was found (Sessions without keys left out)."""
        if self._hits is None:
            pat = self._key_regex()
            hits = {sid: spotlights.key_hits(info, pat) for sid, info in self._session_infos().items()}
            self._hits = {sid: h for sid, h in hits.items() if h}
        return self._hits

    def session_keys(self) -> dict[str, list[str]]:
        """Keys the configured pattern finds per Session, in order: title, git branch, worktree
        path, first prompt (all collected, Key match or not). Sessions without keys are left out."""
        return {sid: list(h) for sid, h in self._key_hits().items()}

    def _key_matches(self) -> dict[str, set[str]]:
        """Per Session, the keys it joins automatically (Key matches); see spotlights.key_matches."""
        return {sid: spotlights.key_matches(h) for sid, h in self._key_hits().items()}

    def _definitions(self) -> list[spotlights.Definition]:
        """User-defined Spotlights, then an Auto-spotlight per key that has a Key match or a Picked
        session (unless a user-defined one has the key as its name). Picks and include edges are
        stored by id."""
        picked: dict[str, set[str]] = {}
        for r in self._conn.execute("SELECT spotlight, session_id FROM spotlight_pins"):
            picked.setdefault(r["spotlight"], set()).add(self.graph.session_of(r["session_id"]))
        edges: dict[str, set[str]] = {}
        for r in self._conn.execute("SELECT spotlight, included FROM spotlight_includes"):
            edges.setdefault(r["spotlight"], set()).add(r["included"])
        users = [(r["id"], r["name"]) for r in
                 self._conn.execute("SELECT id, name FROM spotlights ORDER BY name, id")]
        dismissed: dict[str, set[str]] = {}
        for r in self._conn.execute("SELECT key, session_id FROM spotlight_dismissed"):
            dismissed.setdefault(r["key"], set()).add(self.graph.session_of(r["session_id"]))
        state = self._conn.execute("SELECT key, name, locked FROM auto_spotlights").fetchall()
        custom = {r["key"]: r["name"] for r in state}
        locked = {r["key"] for r in state if r["locked"]}
        has_key = {k for ks in self._key_matches().values() for k in ks}
        has_key |= {i[len(spotlights.AUTO_PREFIX):] for i, sids in picked.items()
                    if i.startswith(spotlights.AUTO_PREFIX) and sids}
        has_key |= locked  # a Locked auto-spotlight stays, even with no members
        found = sorted(has_key - {n for _, n in users} - set(self.dismissed_keys()))
        exist = {i for i, _ in users} | {spotlights.auto_id(k) for k in found}  # an edge to a missing one is ignored

        def inc(i: str) -> frozenset[str]:
            return frozenset(edges.get(i, set()) & exist - {i})
        return ([spotlights.Definition(i, n, "picked", "", frozenset(picked.get(i, ())), inc(i))
                 for i, n in users]
                + [spotlights.Definition(spotlights.auto_id(k), custom.get(k, k), "auto", k,
                                         frozenset(picked.get(spotlights.auto_id(k), ())),
                                         inc(spotlights.auto_id(k)), frozenset(dismissed.get(k, ())), k in locked)
                   for k in found])

    def _spotlight_state(self):
        """(infos, definitions, membership by Spotlight id)."""
        infos = self._session_infos()
        defs = self._definitions()
        by_id = {d.id: d for d in defs}
        matches, carried = self._key_matches(), self.session_keys()
        return infos, defs, {d.id: spotlights.resolve(d, infos, matches, [by_id[i] for i in d.includes], carried)
                             for d in defs}

    def _carrier_index(self) -> dict[str, list[tuple[str, spotlights.KeyHit]]]:
        """Per key, the Sessions carrying it with the strongest hit (fields are searched in key order)."""
        if self._carriers is None:
            idx: dict[str, list[tuple[str, spotlights.KeyHit]]] = {}
            for sid, hits in self._key_hits().items():
                for key, found in hits.items():
                    idx.setdefault(key, []).append((sid, found[0]))
            self._carriers = idx
        return self._carriers

    def _pending(self, defn: spotlights.Definition,
                 members: frozenset[str]) -> list[tuple[str, spotlights.KeyHit]]:
        """Sessions that carry the Auto-spotlight's key but are not in it and not dismissed."""
        return [(sid, hit) for sid, hit in self._carrier_index().get(defn.key, ())
                if sid not in members and sid not in defn.dismissed]

    def _suggested(self, defn: spotlights.Definition, members: frozenset[str],
                   by: dict) -> tuple[SpotlightSuggested, ...]:
        infos = self._session_infos()
        return tuple(sorted((SpotlightSuggested(sid, infos[sid].label, by[sid].cost if sid in by else 0.0,
                                                hit.field, hit.snippet)
                             for sid, hit in self._pending(defn, members)),
                            key=lambda x: (-x.cost, x.session_id)))

    def _pending_by_key(self) -> list[tuple[str, spotlights.Definition | None, str, list]]:
        """(key, its Auto-spotlight or None, name, pending (session, hit) pairs) for every key
        with something pending, keys sorted; keys with no Auto-spotlight yet are included."""
        _, defs, member = self._spotlight_state()
        auto = {d.key: d for d in defs if d.kind == "auto"}
        dismissed: dict[str, set[str]] = {}
        for r in self._conn.execute("SELECT key, session_id FROM spotlight_dismissed"):
            dismissed.setdefault(r["key"], set()).add(self.graph.session_of(r["session_id"]))
        hidden = set(self.dismissed_keys())
        out = []
        for key in sorted(set(self._carrier_index()) - hidden):
            defn = auto.get(key)
            if defn is None:  # nothing joins a key without an Auto-spotlight, so every carrier is a candidate
                defn = spotlights.Definition(spotlights.auto_id(key), key, "auto", key,
                                             dismissed=frozenset(dismissed.get(key, ())))
                members: frozenset[str] = frozenset()
            else:
                members = member[defn.id].members
            found = self._pending(defn, members)
            if found:
                out.append((key, auto.get(key), defn.name, found))
        return out

    def suggestions(self) -> list[SuggestionGroup]:
        """Every pending suggestion across all keys, grouped by key (keys sorted), including keys
        that have no Auto-spotlight yet. Each group lists its Sessions costliest first."""
        by = {s.session_id: s for s in self.session_summaries()}
        infos = self._session_infos()
        return [SuggestionGroup(key, defn.id if defn else None, name, tuple(sorted(
                    (SpotlightSuggested(sid, infos[sid].label, by[sid].cost if sid in by else 0.0,
                                        hit.field, hit.snippet) for sid, hit in found),
                    key=lambda x: (-x.cost, x.session_id))))
                for key, defn, name, found in self._pending_by_key()]

    def session_suggestions(self, session_id: str) -> list[SessionSuggestion]:
        """The keys a Session is suggested for (logical Session id), in key order. No costs computed."""
        sid = self.graph.session_of(session_id)
        return [SessionSuggestion(key, defn.id if defn else None, name, hit.field, hit.snippet)
                for key, defn, name, found in self._pending_by_key()
                for s, hit in found if s == sid]

    def _carried_key(self, ref: str) -> str | None:
        """`ref` as a key some Session carries (upper-cased), for a key with no Auto-spotlight yet."""
        key = ref.strip().upper()
        return key if key in self._carrier_index() else None

    def _find(self, ref: str, defs: list[spotlights.Definition] | None = None) -> spotlights.Definition | None:
        """The Spotlight a reference means: id, then key (Auto-spotlights), then a name that
        exactly one Spotlight has. None when there is none; SpotlightError when a name is ambiguous."""
        defs = self._definitions() if defs is None else defs
        for d in defs:
            if d.id == ref:
                return d
        for d in defs:
            if d.kind == "auto" and d.key == ref:
                return d
        named = [d for d in defs if d.name == ref]
        if len(named) > 1:
            raise spotlights.SpotlightError(
                f"{ref!r} is the name of several Spotlights; use an id: " + ", ".join(d.id for d in named))
        return named[0] if named else None

    def _need(self, ref: str, defs: list[spotlights.Definition] | None = None) -> spotlights.Definition:
        defn = self._find(ref, defs)
        if defn is None:
            raise spotlights.SpotlightError(f"no Spotlight named {ref!r}")
        return defn

    def resolve_spotlight(self, ref: str) -> str:
        """The id of the Spotlight `ref` names (id, then key, then unique name); SpotlightError
        when there is none or a name is ambiguous (the message lists the candidate ids)."""
        return self._need(ref).id

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
            self._local_day(last_ts) if last_ts is not None else None, len(defn.includes),
            defn.id, defn.key, defn.locked)

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
        out = [self._summary(d, member[d.id].members, rows) for d in defs]
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
                       top_n: int = 10, custom: Period | None = None) -> list[SpotlightRank]:
        """Spotlights ranked by spend inside the Period (same periods as period_comparison)."""
        cur, _, _ = resolve_periods(mode, self.today(now), include_today, custom)
        lo, hi = self._bounds(cur)
        _, defs, member = self._spotlight_state()
        agg = self._session_aggregates(lo, hi)
        ranked = []
        for d in defs:
            members = member[d.id].members
            mine = [agg[sid] for sid in members if sid in agg]
            period_cost = sum(a.period_cost for a in mine)
            if period_cost > 0:
                ranked.append(SpotlightRank(self._spotlight_summary(
                    d, sum(a.cost for a in mine), sum(a.requests for a in mine), len(members),
                    min(a.first for a in mine), max(a.last for a in mine)), period_cost))
        ranked.sort(key=lambda x: (-x.period_cost, x.summary.name))
        return ranked[:top_n]

    def spotlight(self, ref: str) -> SpotlightDetail:
        """One Spotlight in full, by id, key or unique name; KeyError if there is none,
        SpotlightError if the name is ambiguous."""
        infos, defs, member = self._spotlight_state()
        defn = self._find(ref, defs)
        if defn is None:
            raise KeyError(ref)
        name = defn.id
        ms = member[name]
        rows = self._request_rows()
        summary = self._summary(defn, ms.members, rows)
        in_other: dict[str, list[spotlights.Definition]] = {}
        for other_def in defs:
            other = other_def.id
            if other != name:
                for sid in member[other].members & ms.members:
                    if other in defn.includes:
                        continue
                    in_other.setdefault(sid, []).append(other_def)
        by = {s.session_id: s for s in self.session_summaries()}
        members = [SpotlightSession(sid, infos[sid].label, by[sid].cost, by[sid].subagent_cost,
                                 by[sid].requests, self._joined_as(sid, infos, ms, defn),
                                 tuple(sorted(o.name for o in in_other.get(sid, ()))), sid in defn.picked,
                                 sid in ms.key_matched, sid in defn.picked or sid in ms.key_matched,
                                 ms.via.get(sid),
                                 also_in_ids=tuple(o.id for o in sorted(in_other.get(sid, ()),
                                                                        key=lambda o: o.name)))
                for sid in ms.members if sid in by]
        sessions = self._session_tree(members, {sid: infos[sid].parent for sid in ms.members})
        related = tuple(sorted(
            (SpotlightRelated(sid, infos[sid].label, by[sid].cost if sid in by else 0.0,
                              infos[sid].parent or "") for sid in ms.related),
            key=lambda x: (-x.cost, x.session_id)))
        subs = tuple(SpotlightSubagent(a.session_id, a.agent_id, a.agent_type, a.description, a.cost)
                     for a in self.subagent_summaries() if a.session_id in ms.members)
        agent_types = {(a.session_id, a.agent_id): a.agent_type or "unknown"
                       for a in self.subagent_summaries()}
        days: dict[date, _DayAcc] = {}
        comps = {n: 0.0 for n, _ in COMPONENTS}
        for r in rows:
            if r["sid"] not in ms.members:
                continue
            d = days.setdefault(self._local_day(r["ts"]), _DayAcc())
            total = 0.0
            for n, c in COMPONENTS:
                d.components[n] += r[c]
                comps[n] += r[c]
                total += r[c]
            d.cost += total
            d.by_session[r["sid"]] = d.by_session.get(r["sid"], 0.0) + total
            if r["agent"] is not None:
                key = (r["sid"], r["agent"])
                d.by_run[key] = d.by_run.get(key, 0.0) + total
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
            blank = _DayAcc()
            out_days = tuple(
                SpotlightDay(day, (c := days.get(day, blank)).cost, c.by_session, day_subagent(c.by_run),
                             c.components)
                for day in (first + timedelta(n) for n in range((last - first).days + 1)))
        by_id = {d.id: d for d in defs}
        included = tuple(
            SpotlightIncluded(by_id[i].name, by_id[i].kind,
                              (s := self._summary(by_id[i], member[i].members, rows)).total, s.sessions, i)
            for i in sorted(defn.includes, key=lambda i: (by_id[i].name, i)))
        dismissed = tuple(sorted(
            (SpotlightDismissed(sid, infos[sid].label, by[sid].cost if sid in by else 0.0)
             for sid in ms.dismissed), key=lambda x: (-x.cost, x.session_id)))
        suggested = self._suggested(defn, ms.members, by) if defn.kind == "auto" else ()
        return SpotlightDetail(summary, sessions, subs, related, out_days, comps,
                               tuple(sorted(defn.picked)), included, dismissed, suggested)

    def spotlight_comparison(self, refs: list[str]) -> list[SpotlightDetail]:
        """Two or more Spotlights side by side, each in full (never summed). KeyError if unknown."""
        defs = self._definitions()
        ids = []
        for ref in refs:
            defn = self._find(ref, defs)
            if defn is None:
                raise KeyError(ref)
            ids.append(defn.id)
        ids = list(dict.fromkeys(ids))
        if len(ids) < 2:
            raise ValueError("select at least two different Spotlights")
        return [self.spotlight(i) for i in ids]

    @staticmethod
    def _session_tree(rows: list[SpotlightSession],
                      parents: dict[str, str | None]) -> tuple[SpotlightSession, ...]:
        """Rows in tree order: parent links restricted to the members, costliest subtree first."""
        by = {r.session_id: r for r in rows}
        members_under: dict[str | None, list[str]] = {}
        for sid in by:
            parent = parents.get(sid)
            members_under.setdefault(parent if parent in by and parent != sid else None, []).append(sid)
        total: dict[str, float] = {}

        def subtree(sid: str, seen: frozenset[str]) -> float:
            if sid not in total:
                total[sid] = by[sid].cost + sum(
                    subtree(k, seen | {sid}) for k in members_under.get(sid, ()) if k not in seen)
            return total[sid]

        out: list[SpotlightSession] = []

        def walk(parent: str | None, depth: int) -> None:
            for sid in sorted(members_under.get(parent, ()), key=lambda k: (-subtree(k, frozenset()), k)):
                out.append(dataclasses.replace(by[sid], parent_id=parent, depth=depth, subtree_cost=total[sid]))
                walk(sid, depth + 1)
        walk(None, 0)
        return tuple(out)

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
        if sid in ms.via:
            return f"Included spotlight {ms.via[sid]}"
        parent = infos[info.parent].label if info.parent in infos else ""
        return f"Side session of {parent}"

    # Editing user-defined Spotlights (Picked sessions live in the store, not config.toml).

    def _insert_pins(self, sp_id: str, session_ids: Iterable[str]) -> None:
        """Add Picked sessions to the Spotlight with id `sp_id`. Call inside a transaction."""
        self._conn.executemany("INSERT OR IGNORE INTO spotlight_pins VALUES (?, ?)",
                               [(sp_id, self.graph.session_of(s)) for s in session_ids])

    @staticmethod
    def _new_id() -> str:
        return "sp-" + uuid.uuid4().hex[:8]

    def _check_include(self, sp_id: str, other: str, defs: list, creating: bool = False) -> None:
        """Raise SpotlightError unless Spotlight `sp_id` may include `other` (ids): one level of
        nesting, an Auto-spotlight cannot include, nothing includes itself. `creating`: `sp_id` is
        being made as a user-defined Spotlight, so it need not exist yet."""
        by = {d.id: d for d in defs}
        if other == sp_id:
            raise spotlights.SpotlightError("a Spotlight cannot include itself")
        if not creating and by[sp_id].kind == "auto":
            raise spotlights.SpotlightError("an Auto-spotlight cannot include other Spotlights")
        name = by[sp_id].name if sp_id in by else ""
        if by[other].includes:
            raise spotlights.SpotlightError(
                f"{by[other].name!r} includes other Spotlights, so it cannot be included")
        includer = next((d.name for d in defs if sp_id in d.includes), None)
        if includer:
            raise spotlights.SpotlightError(
                f"{name!r} is included in {includer!r}, so it cannot include other Spotlights")

    def _require_sessions(self, session_ids: Iterable[str]) -> None:
        known = self._session_infos()
        for sid in session_ids:
            if self.graph.session_of(sid) not in known:
                raise spotlights.SpotlightError(f"no Session with id {sid!r}")

    def create_spotlight(self, name: str, session_ids: Iterable[str] = (),
                         includes: Iterable[str] = ()) -> str:
        """Create a Spotlight (empty with just a name), add Picked sessions and Included
        spotlights (by id, key or name) to it; returns its new id. Names need not be unique (ids
        tell Spotlights apart). Nothing is saved when a Session or included Spotlight is unknown or
        an include breaks a nesting rule."""
        name = name.strip()
        if not name:
            raise spotlights.SpotlightError("a Spotlight needs a name")
        defs = self._definitions()
        sp_id = self._new_id()
        include_ids = list(dict.fromkeys(self._need(o, defs).id for o in includes))
        for other in include_ids:
            self._check_include(sp_id, other, defs, creating=True)
        session_ids = list(session_ids)
        self._require_sessions(session_ids)
        with self._conn:
            self._conn.execute("INSERT INTO spotlights (name, id) VALUES (?, ?)", (name, sp_id))
            self._insert_pins(sp_id, session_ids)
            self._conn.executemany("INSERT OR IGNORE INTO spotlight_includes VALUES (?, ?)",
                                   [(sp_id, o) for o in include_ids])
        return sp_id

    def rename_spotlight(self, ref: str, name: str) -> None:
        """Give any Spotlight a new, non-empty name (it need not be unique). An Auto-spotlight
        keeps collecting sessions by its key; its picks, includes and the edges to it are by id."""
        name = name.strip()
        if not name:
            raise spotlights.SpotlightError("a Spotlight needs a name")
        defn = self._need(ref)
        with self._conn:
            if defn.kind == "auto":
                store.set_auto_state(self._conn, defn.key, name=name)
            else:
                self._conn.execute("UPDATE spotlights SET name = ? WHERE id = ?", (name, defn.id))

    def _need_auto(self, ref: str) -> spotlights.Definition:
        defn = self._need(ref)
        if defn.kind != "auto":
            raise spotlights.SpotlightError(f"{defn.name!r} is not an Auto-spotlight")
        return defn

    def lock_spotlight(self, ref: str) -> None:
        """Lock an Auto-spotlight: its current Key matches become Picked sessions (so the total
        does not change) and from then on every Session carrying its key is only a Suggested session."""
        defn = self._need_auto(ref)
        if defn.locked:
            return
        matched = spotlights.direct_members(defn, self._session_infos(), self._key_matches())[0]
        with self._conn:
            store.set_auto_state(self._conn, defn.key, locked=True)
            self._insert_pins(defn.id, sorted(matched))

    def unlock_spotlight(self, ref: str) -> None:
        """Unlock an Auto-spotlight: Key matches join automatically again; Dismissed sessions stay dismissed."""
        defn = self._need_auto(ref)
        with self._conn:
            store.set_auto_state(self._conn, defn.key, locked=False)

    def include_spotlight(self, ref: str, other: str) -> None:
        """Make Spotlight `ref` include Spotlight `other` (see the nesting rules in _check_include)."""
        defs = self._definitions()
        other_def = self._need(other, defs)
        sp = self._need(ref, defs)
        self._check_include(sp.id, other_def.id, defs)
        with self._conn:
            self._conn.execute("INSERT OR IGNORE INTO spotlight_includes VALUES (?, ?)", (sp.id, other_def.id))

    def exclude_spotlight(self, ref: str, other: str) -> None:
        defs = self._definitions()
        sp, other_def = self._need(ref, defs), self._need(other, defs)
        with self._conn:
            self._conn.execute("DELETE FROM spotlight_includes WHERE spotlight = ? AND included = ?",
                               (sp.id, other_def.id))

    def includable_spotlight_choices(self, ref: str) -> list[tuple[str, str]]:
        """(id, name) of the Spotlights `ref` may still include, by name; none when `ref` may not
        include at all."""
        defs = self._definitions()
        me = self._find(ref, defs)
        if me is None or me.kind == "auto" or any(me.id in d.includes for d in defs):
            return []
        return sorted(((d.id, d.name) for d in defs
                       if d.id != me.id and not d.includes and d.id not in me.includes),
                      key=lambda c: (c[1], c[0]))

    def includable_spotlights(self, ref: str) -> list[str]:
        """Names of the Spotlights `ref` may still include."""
        return [n for _, n in self.includable_spotlight_choices(ref)]

    def delete_spotlight(self, ref: str) -> None:
        sp_id = self._need(ref).id
        with self._conn:
            self._conn.execute("DELETE FROM spotlights WHERE id = ?", (sp_id,))
            self._conn.execute("DELETE FROM spotlight_pins WHERE spotlight = ?", (sp_id,))
            self._conn.execute("DELETE FROM spotlight_includes WHERE spotlight = ? OR included = ?",
                               (sp_id, sp_id))

    def add_session(self, ref: str, session_id: str) -> None:
        self.add_sessions(ref, [session_id])

    def add_sessions(self, ref: str, session_ids: Iterable[str], create: bool = True) -> None:
        """Add Picked sessions to a Spotlight (by id, key or unique name). A reference that
        matches no Spotlight creates a user-defined one with that name, unless `create` is False,
        which makes it an error."""
        ref = ref.strip()
        if not ref:
            raise spotlights.SpotlightError("a Spotlight needs a name")
        session_ids = list(session_ids)
        self._require_sessions(session_ids)
        defn = self._find(ref)
        key = self._carried_key(ref) if defn is None else None  # accepting a suggestion creates its Auto-spotlight
        if key is not None:
            carry = [s for s in session_ids if key in self.session_keys().get(self.graph.session_of(s), ())]
            if len(carry) < len(session_ids):
                if create:
                    key = None  # not a suggestion: the name is just a new user-defined Spotlight's
                else:
                    missing = next(s for s in session_ids if s not in carry)
                    raise spotlights.SpotlightError(f"Session {missing!r} does not carry {key}")
        if defn is None and key is None and not create:
            raise spotlights.SpotlightError(f"no Spotlight named {ref!r}")
        with self._conn:
            if key is not None:
                sp_id = spotlights.auto_id(key)
                self._conn.executemany("DELETE FROM spotlight_dismissed WHERE key = ? AND session_id = ?",
                                       [(key, self.graph.session_of(s)) for s in session_ids])
            elif defn is None:
                sp_id = self._new_id()
                self._conn.execute("INSERT INTO spotlights (name, id) VALUES (?, ?)", (ref, sp_id))
            else:
                sp_id = defn.id
            self._insert_pins(sp_id, session_ids)
            if defn is not None and defn.kind == "auto":  # picking a Dismissed session clears its dismissal
                self._conn.executemany("DELETE FROM spotlight_dismissed WHERE key = ? AND session_id = ?",
                                       [(defn.key, self.graph.session_of(s)) for s in session_ids])

    def remove_session(self, ref: str, session_id: str) -> None:
        """Remove a Session from a Spotlight; Side sessions that joined through it leave with it.
        In an Auto-spotlight a Session that carries the key becomes a Dismissed session (and is
        unpicked); any other is just unpicked."""
        sid = self.graph.session_of(session_id)
        defn = self._find(ref)
        if defn is None and (key := self._carried_key(ref)) is not None:  # a suggestion for a key with no Auto-spotlight
            if key not in self.session_keys().get(sid, ()):
                raise spotlights.SpotlightError(f"Session {session_id!r} does not carry {key}")
            with self._conn:
                self._conn.execute("INSERT OR IGNORE INTO spotlight_dismissed VALUES (?, ?)", (key, sid))
            return
        defn = self._need(ref)
        carries = defn.kind == "auto" and defn.key in self.session_keys().get(sid, ())
        with self._conn:
            self._conn.execute("DELETE FROM spotlight_pins WHERE spotlight = ? AND session_id IN (?, ?)",
                               (defn.id, session_id, sid))
            if carries:
                self._conn.execute("INSERT OR IGNORE INTO spotlight_dismissed VALUES (?, ?)", (defn.key, sid))

    def dismissed_keys(self) -> list[str]:
        """Keys whose Auto-spotlight the user hid, sorted."""
        return [r["key"] for r in self._conn.execute("SELECT key FROM spotlight_dismissed_keys ORDER BY key")]

    def dismissed_key_rows(self) -> list[tuple[str, str, str]]:
        """(key, Spotlight id, name) of each Dismissed key, so the user can recognise and restore it."""
        custom = {r["key"]: r["name"] for r in self._conn.execute("SELECT key, name FROM auto_spotlights")}
        return [(k, spotlights.auto_id(k), custom.get(k, k)) for k in self.dismissed_keys()]

    def dismiss_key(self, ref: str) -> None:
        """Hide a key's Auto-spotlight (by id or key) from lists, the Dashboard, Compare and triage.
        Its name, picks and Dismissed sessions stay; include edges to it are ignored meanwhile."""
        defn = self._find(ref)
        if defn is not None and defn.kind != "auto":
            raise spotlights.SpotlightError(f"{defn.name!r} is not an Auto-spotlight")
        key = defn.key if defn is not None else self._carried_key(ref)
        if key is None:
            raise spotlights.SpotlightError(f"no key {ref!r}")
        with self._conn:
            self._conn.execute("INSERT OR IGNORE INTO spotlight_dismissed_keys VALUES (?)", (key,))

    def restore_key(self, key: str) -> None:
        """Show a Dismissed key's Auto-spotlight again, with everything it had."""
        key = key.strip().upper()
        if key not in self.dismissed_keys():
            raise spotlights.SpotlightError(f"{key!r} is not a Dismissed key")
        with self._conn:
            self._conn.execute("DELETE FROM spotlight_dismissed_keys WHERE key = ?", (key,))

    def restore_session(self, ref: str, session_id: str) -> None:
        """Undo the dismissal of a Session in an Auto-spotlight (by id, key or unique name)."""
        defn = self._need(ref)
        if defn.kind != "auto":
            raise spotlights.SpotlightError(f"{defn.name!r} is not an Auto-spotlight")
        sid = self.graph.session_of(session_id)
        if sid not in defn.dismissed:
            raise spotlights.SpotlightError(f"Session {session_id!r} is not dismissed from {defn.name!r}")
        with self._conn:
            self._conn.execute("DELETE FROM spotlight_dismissed WHERE key = ? AND session_id = ?",
                               (defn.key, sid))

    def session_labels(self) -> dict[str, str]:
        """Label of every logical Session id: custom title, else the first 60 characters of the
        first prompt, else the id. The single label source every page (and Top sessions) uses."""
        return {sid: info.label for sid, info in self._session_infos().items()}

    SEARCH_LIMIT = 10

    def search_sessions(self, query: str, name: str,
                        exclude: Iterable[str] = ()) -> list[SessionCandidate]:
        """Sessions to pick for the Spotlight `name` (an id, key or name; one with no Spotlight yet
        has no members).

        Candidates are every Session except the Spotlight's direct members and `exclude`. An empty
        query returns the suggested Forks (unpicked Forks of members), costliest first. Otherwise
        the query is matched as a case-insensitive substring of the label or the id; suggested
        Forks rank first, then the better match, then the costlier Session; at most 10 are returned.
        """
        infos, defs, member = self._spotlight_state()
        try:
            defn = self._find(name, defs)
        except spotlights.SpotlightError:
            defn = None
        ms = member.get(defn.id) if defn else None
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

    # Sessions and Breakdowns: the dimension registry and slicing live in explore.py
    def explore(self, spec: explore.ExploreSpec) -> explore.ExploreResult:
        """Cost and requests per value of `spec.group`, over the requests the filters keep."""
        return explore.run(self._conn, self._local_day, spec, self.findings, self.session_labels())

    def sessions(self, spec: explore.ExploreSpec) -> explore.SessionsResult:
        """One row per logical Session over the requests the filters keep (the group-by is ignored)."""
        return explore.sessions(self._conn, self._local_day, self.local_time,
                                self.session_labels(), spec, self.findings)

    def value_names(self, key: str) -> dict[str, str]:
        """Readable names of a dimension's values: Project names and Session labels."""
        if key == "project":
            return self.project_names()
        if key == "session":
            return self.session_labels()
        return {}

    def filter_options(self, spec: explore.ExploreSpec, key: str, q: str = "",
                       limit: int = 100) -> list[explore.FilterOption]:
        """Values of dimension `key` for the filter popover (see `explore.filter_options`)."""
        return explore.filter_options(self._conn, self._local_day, spec, key, q,
                                      self.value_names, self.findings, limit)
