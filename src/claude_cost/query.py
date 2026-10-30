"""Query service: the one interface the web server and CLI use for every number."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
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


class QueryService:
    def __init__(self, conn: sqlite3.Connection, timezone: str | None = None):
        self._conn = conn
        self._tz: tzinfo | None = ZoneInfo(timezone) if timezone else None

    def _local_day(self, epoch: float) -> date:
        return datetime.fromtimestamp(epoch, self._tz).date()

    @staticmethod
    def _filter(surface: str | None, data_root: str | Path | None) -> tuple[str, list]:
        """WHERE clause (over `requests r`) for the Surface and Data root filters."""
        where, args = [], []
        if surface is not None:
            where.append("r.message_id IN (SELECT message_id FROM request_attrs WHERE surface = ?)")
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
            f"LEFT JOIN request_attrs a ON a.message_id = r.message_id{where} "
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
            "FROM requests r JOIN request_attrs a ON a.message_id = r.message_id "
            "LEFT JOIN session_links l ON l.stem = r.session_id "
            "LEFT JOIN subagents s ON s.session_stem = r.session_id AND s.agent_id = a.agent_id"
            f"{where}{' AND' if where else ' WHERE'} a.agent_id IS NOT NULL "
            "GROUP BY r.session_id, a.agent_id ORDER BY first, sid, a.agent_id", args).fetchall()
        return [SubagentSummary(r["sid"], r["agent_id"], r["agent_type"], bool(r["is_fork"]),
                                r["tool_use_id"], r["parent_agent_id"], r["description"],
                                r["cost"], r["n"]) for r in rows]

    def unpriced_models(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT model FROM requests WHERE priced = 0 ORDER BY model")
        return [r["model"] for r in rows]
