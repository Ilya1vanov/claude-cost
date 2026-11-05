"""Findings framework: Drains and Opportunities.

A finding is a `FindingDef`: an id, a kind, a formula text, optional thresholds and a
`detect(ctx)` function that returns the requests it covers (and each one's avoidable $).
The framework turns that into a `Finding` with Attributed spend, Avoidable spend (clamped
at $0, a lower bound) and the Sessions and Requests it covers. See conventions.md.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from typing import Callable, Mapping

from .. import compact, pricing, session_graph
from ..localtime import local_day
from ..sql import COST_SQL, SESSION_ID_SQL, SESSION_JOIN, context_tokens

DRAIN, OPPORTUNITY = "drain", "opportunity"
DEFAULT_COMPARE_MODEL = "claude-opus-5-5"  # Fable usage compares against it (fable_compare_model)
NOT_ADDITIVE_NOTE = ("Avoidable figures are lower bounds and are not additive: findings overlap, "
                     "so they are never summed.")


@dataclass(frozen=True)
class Threshold:
    """A configurable number. Becomes a config.toml setting of the same name automatically."""
    name: str  # e.g. "cold_resume_idle_minutes"
    default: int
    doc: str
    minimum: int = 0


# Thresholds that belong to no single finding (Compact analysis, timeline, web refresh); they
# are config settings like the finding thresholds and reach the query service the same way.
CORE_THRESHOLDS = (
    Threshold("compact_min_sample", compact.MIN_SAMPLE,
              "Compactions a model needs before it gets its own Compact threshold.", 1),
    Threshold("compact_step", compact.STEP,
              "Compact threshold candidates are multiples of this many tokens.", 1),
    Threshold("timeline_idle_gap_seconds", 300,
              "Timeline: a gap longer than this many seconds is marked as an idle gap.", 1),
    Threshold("refresh_stale_seconds", 60,
              "Web: a page load re-ingests when the last ingest is older than this many seconds.", 0),
)


@dataclass(frozen=True)
class RequestRow:
    """One Period Request with everything detectors need. `gap` = seconds since the previous
    Request of its Stream (None for the Stream's first); `stream_first_context` = context of
    that Stream's first Request. Both are stored at ingest, so they may predate the Period."""
    message_id: str
    session_id: str  # logical Session
    project: str
    ts: float
    day: date  # local day
    model: str
    input: int
    output: int
    cache_read: int
    write_5m: int
    write_1h: int
    cost: float
    write_cost: float
    cache_event: str  # cold | warm | none
    speed: str | None
    inference_geo: str | None
    gap: float | None
    stream_first_context: int
    agent_id: str | None = None  # the Subagent that made the request (None = main transcript)
    agent_type: str | None = None
    agent_fork: bool = False  # that Subagent is a fork (subagent metadata)

    @property
    def context(self) -> int:
        return context_tokens(self.input, self.cache_read, self.write_5m, self.write_1h)


@dataclass(frozen=True)
class Covered:
    """A request a finding covers; `avoidable` is its raw avoidable $ (may be negative)."""
    message_id: str
    avoidable: float | None = None


@dataclass(frozen=True)
class CollapsibleCell:
    """A table cell that shows `summary` and folds `items` into a collapsed list."""
    summary: str
    items: tuple[str, ...]


@dataclass(frozen=True)
class DetailTable:
    """Structured detail for a finding's page: column names and rows (a cell is text or a
    CollapsibleCell). Shown on the finding detail page only."""
    columns: tuple[str, ...]
    rows: tuple[tuple[str | CollapsibleCell, ...], ...]


@dataclass(frozen=True)
class Detection:
    covered: list[Covered]
    detail: str = ""  # measured inputs to the formula, e.g. "median first context 12,000"
    table: DetailTable | None = None


@dataclass(frozen=True)
class FindingDef:
    id: str  # unique, URL-safe: "cold-resume"
    kind: str  # DRAIN | OPPORTUNITY
    title: str
    description: str
    formula: str | None  # None = Attributed spend only (no avoidable figure)
    detect: Callable[["FindingContext"], Detection]
    thresholds: tuple[Threshold, ...] = ()


@dataclass(frozen=True)
class SessionCoverage:
    session_id: str
    attributed: float
    avoidable: float | None  # clamped at 0
    requests: int


@dataclass(frozen=True)
class CoveredRequest:
    message_id: str
    session_id: str
    project: str
    ts: float
    model: str
    cost: float
    avoidable: float | None  # raw, may be negative
    agent_id: str | None = None


@dataclass(frozen=True)
class SubagentCoverage:
    """A Subagent a finding covers (drill-down for Subagent findings)."""
    agent_id: str
    session_id: str
    agent_type: str | None
    attributed: float
    requests: int


@dataclass(frozen=True)
class Finding:
    id: str
    kind: str
    title: str
    description: str
    formula: str | None
    detail: str
    attributed: float
    avoidable: float | None  # lower bound, never below 0; None = not estimated
    sessions: tuple[SessionCoverage, ...]
    requests: tuple[CoveredRequest, ...]
    subagents: tuple[SubagentCoverage, ...] = ()
    table: DetailTable | None = None


class FindingContext:
    """What a detector sees: the Period's Requests (`rows`) and the facts stored per Stream at
    ingest, never all history; plus thresholds, prices and the time zone."""

    def __init__(self, rows: list[RequestRow], thresholds: Mapping[str, int],
                 defs: Mapping[str, Threshold], price_table: Mapping | None,
                 conn: sqlite3.Connection | None = None, compact=None,
                 compare_model: str = DEFAULT_COMPARE_MODEL, *,
                 median_first_context: float | None, graph=None,
                 period_start: float | None = None, period_end: float | None = None):
        self.rows = rows
        # the Period's own bounds (end exclusive); None on a side that is all history
        self.period_start, self.period_end = period_start, period_end
        # lifetime, not per Period; None when the store has not stored it (Cold resume then reports nothing)
        self.median_first_context = median_first_context
        if graph is None and conn is not None:
            graph = session_graph.load(conn)
        self.graph = graph  # session_graph.SessionGraph: how Transcripts group into Sessions
        self.conn = conn  # for detectors that need data beyond requests (e.g. Listings)
        self.compact = compact  # compact.CompactAnalysis: learned Compact thresholds
        self.price_table = price_table
        self.compare_model = compare_model  # model the Fable usage Opportunity prices against
        self._thresholds, self._defs = thresholds, defs

    def threshold(self, name: str) -> int:
        return self._thresholds.get(name, self._defs[name].default)

    def price(self, model: str, r: RequestRow) -> pricing.CostComponents | None:
        """What `r`'s tokens would cost on `model` (same date, speed and region)."""
        return pricing.price_request(
            model, r.day, input=r.input, output=r.output, cache_read=r.cache_read,
            write_5m=r.write_5m, write_1h=r.write_1h, speed=r.speed,
            inference_geo=r.inference_geo, table=self.price_table)


def load_rows(conn: sqlite3.Connection, tz: tzinfo | None, lo: float | None,
              hi: float | None) -> list[RequestRow]:
    """The Requests from `lo` up to `hi` (all when None), oldest first, with their stored Stream facts."""
    where, args = [], []
    if lo is not None:
        where.append("r.ts_epoch >= ?")
        args.append(lo)
    if hi is not None:
        where.append("r.ts_epoch < ?")
        args.append(hi)
    sql = (f"SELECT r.message_id, {SESSION_ID_SQL} AS sid, "
           "r.project, r.ts_epoch, r.model, r.input_tokens, r.output_tokens, r.cache_read_tokens, "
           "r.cache_write_5m_tokens AS w5, r.cache_write_1h_tokens AS w1, "
           f"{COST_SQL} AS cost, r.cost_cache_write_5m + r.cost_cache_write_1h AS wcost, "
           "COALESCE(c.event, 'none') AS ev, a.speed, a.inference_geo, "
           "st.gap, COALESCE(st.first_context, 0) AS first_context, "
           "sv.agent_id AS agent_id "
           f"FROM requests r {SESSION_JOIN} "
           "LEFT JOIN request_cache c ON c.message_id = r.message_id "
           "LEFT JOIN request_stream st ON st.message_id = r.message_id "
           "LEFT JOIN request_attrs a ON a.message_id = r.message_id "
           "LEFT JOIN request_surface sv ON sv.message_id = r.message_id "
           + ("WHERE " + " AND ".join(where) + " " if where else "")
           + "ORDER BY r.ts_epoch, r.message_id")
    found = conn.execute(sql, args).fetchall()
    agents = json.dumps(sorted({x["agent_id"] for x in found if x["agent_id"]}))
    kinds = {a["agent_id"]: a for a in conn.execute(  # only the Period's Subagents
        "SELECT agent_id, MAX(agent_type) AS agent_type, MAX(is_fork) AS is_fork "
        "FROM subagent_files WHERE agent_id IN (SELECT value FROM json_each(?)) GROUP BY agent_id",
        (agents,))}
    return [RequestRow(
        x["message_id"], x["sid"], x["project"], x["ts_epoch"], local_day(x["ts_epoch"], tz),
        x["model"], x["input_tokens"], x["output_tokens"], x["cache_read_tokens"],
        x["w5"], x["w1"], x["cost"], x["wcost"], x["ev"], x["speed"] or None,
        x["inference_geo"] or None, x["gap"], x["first_context"],
        x["agent_id"], kinds[x["agent_id"]]["agent_type"] if x["agent_id"] in kinds else None,
        bool(x["agent_id"] in kinds and kinds[x["agent_id"]]["is_fork"]))
        for x in found]


def build_finding(d: FindingDef, det: Detection, rows: Mapping[str, RequestRow]) -> Finding:
    """Attributed = measured cost of covered requests; avoidable = max(0, sum), a lower bound."""
    covered = [(rows[c.message_id], c) for c in det.covered]
    by_session: dict[str, list] = {}
    for r, c in covered:
        by_session.setdefault(r.session_id, []).append((r, c))
    has_avoidable = d.formula is not None
    sessions = tuple(sorted(
        (SessionCoverage(sid, sum(r.cost for r, _ in rc),
                         max(0.0, sum(c.avoidable or 0.0 for _, c in rc)) if has_avoidable else None,
                         len(rc)) for sid, rc in by_session.items()),
        key=lambda s: (-s.attributed, s.session_id)))
    requests = tuple(CoveredRequest(r.message_id, r.session_id, r.project, r.ts, r.model, r.cost,
                                    c.avoidable if has_avoidable else None, r.agent_id)
                     for r, c in sorted(covered, key=lambda rc: (rc[0].ts, rc[0].message_id)))
    agents: dict[str, list[RequestRow]] = {}
    for r, _ in covered:
        if r.agent_id:
            agents.setdefault(r.agent_id, []).append(r)
    subagents = tuple(sorted(
        (SubagentCoverage(a, rs[0].session_id, rs[0].agent_type, sum(r.cost for r in rs), len(rs))
         for a, rs in agents.items()),
        key=lambda s: (-s.attributed, s.agent_id)))
    return Finding(
        d.id, d.kind, d.title, d.description, d.formula, det.detail,
        sum(r.cost for r, _ in covered),
        max(0.0, sum(c.avoidable or 0.0 for _, c in covered)) if has_avoidable else None,
        sessions, requests, subagents, det.table)
