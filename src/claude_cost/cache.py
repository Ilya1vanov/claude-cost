"""Cache event classification: every cache write is Cold or Warm.

Cold write: the session had earlier requests and sat idle longer than the cache TTL since
the previous one, so the whole write rebuilds context the cache already held. Warm write:
anything else (the first request of a session, or growth while the cache was alive).
The TTL is that of the session's most recent write: 1h if it wrote 1h tokens, else 5m.
A request counts as a whole, so a cold request's entire write is cold spend.

The result is stored per request in `request_cache.event` ('cold' | 'warm' | 'none'),
where 'none' means the request wrote nothing. Reads are a separate event type, derived
from `cache_read_tokens` / `cost_cache_read`, so they need no classification.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass

from .sql import SESSION_ID_SQL, SESSION_JOIN, context_tokens

TTL_5M = 5 * 60
TTL_1H = 60 * 60
COLD, WARM, NONE = "cold", "warm", "none"
MEDIAN_FIRST_META = "median_first_context"  # the `meta` row holding the lifetime baseline
BARE_SUBAGENT_META = "bare_subagent_context"  # the `meta` row holding the Bare subagent context
META_ROWS = (MEDIAN_FIRST_META, BARE_SUBAGENT_META)  # `classify_all` stores these; a missing one is stale
BARE_MIN_RUNS = 20  # non-fork runs a subagent type needs to count as the leanest


def classify_session(requests: list[tuple[str, float, int, int]]) -> dict[str, str]:
    """`requests` = (message_id, ts_epoch, write_5m_tokens, write_1h_tokens), any order."""
    events: dict[str, str] = {}
    prev_ts: float | None = None
    ttl = TTL_5M
    for mid, ts, w5m, w1h in sorted(requests, key=lambda r: (r[1], r[0])):
        if w5m + w1h == 0:
            events[mid] = NONE
        else:
            cold = prev_ts is not None and ts - prev_ts > ttl
            events[mid] = COLD if cold else WARM
            ttl = TTL_1H if w1h > 0 else TTL_5M
        prev_ts = ts
    return events


def needs_classification(conn: sqlite3.Connection) -> bool:
    """True when a Request lacks its stored cache event or Stream facts, or the median is absent."""
    return bool(conn.execute(
        "SELECT EXISTS (SELECT 1 FROM requests r "
        "LEFT JOIN request_cache c ON c.message_id = r.message_id "
        "LEFT JOIN request_stream s ON s.message_id = r.message_id "
        "WHERE c.message_id IS NULL OR s.message_id IS NULL) "
        f"OR (SELECT COUNT(*) FROM meta WHERE key IN ({','.join('?' * len(META_ROWS))})) < {len(META_ROWS)}",
        META_ROWS).fetchone()[0])


def invalidate(conn: sqlite3.Connection, message_ids) -> None:
    """Forget the stored rows of these Requests, so the next `classify_all` recomputes their Streams."""
    ids = [(m,) for m in message_ids]
    conn.executemany("DELETE FROM request_cache WHERE message_id = ?", ids)
    conn.executemany("DELETE FROM request_stream WHERE message_id = ?", ids)


def invalidate_transcripts(conn: sqlite3.Connection, stems) -> None:
    """Same, for every Request of these transcripts (their Session grouping changed)."""
    ids = [r[0] for r in conn.execute(
        "SELECT message_id FROM requests WHERE session_id IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted(stems)),))]
    invalidate(conn, ids)


def classify_all(conn: sqlite3.Connection) -> None:
    """Classify the Streams (a logical Session's main Stream, a Subagent separately) that hold a
    Request without stored rows, then refresh the lifetime median; the other Streams are untouched.
    The result equals a full recompute (delete both tables first).

    The walk over each Stream in time order stores the facts findings need from before their
    Period (`request_stream`: each Request's idle gap and its Stream's first-request context)
    next to the cache events (`request_cache`)."""
    dirty = json.dumps([r[0] for r in conn.execute(
        f"SELECT DISTINCT {SESSION_ID_SQL} FROM requests r {SESSION_JOIN} "
        "LEFT JOIN request_cache c ON c.message_id = r.message_id "
        "LEFT JOIN request_stream s ON s.message_id = r.message_id "
        "WHERE c.message_id IS NULL OR s.message_id IS NULL")])
    streams: dict[tuple[str, str | None], list[tuple[str, float, int, int]]] = {}
    context: dict[str, int] = {}
    for r in conn.execute(
            "SELECT r.message_id, r.ts_epoch, r.cache_write_5m_tokens AS w5, "
            f"r.cache_write_1h_tokens AS w1, {SESSION_ID_SQL} AS sid, "
            "r.input_tokens AS inp, r.cache_read_tokens AS cr, a.agent_id AS agent "
            f"FROM requests r {SESSION_JOIN} "
            "LEFT JOIN request_surface a ON a.message_id = r.message_id "
            f"WHERE {SESSION_ID_SQL} IN (SELECT value FROM json_each(?))", (dirty,)):
        streams.setdefault((r["sid"], r["agent"]), []).append(  # a Subagent has its own cache
            (r["message_id"], r["ts_epoch"], r["w5"], r["w1"]))
        context[r["message_id"]] = context_tokens(r["inp"], r["cr"], r["w5"], r["w1"])
    events, facts = [], []
    for reqs in streams.values():
        reqs.sort(key=lambda r: (r[1], r[0]))
        events += classify_session(reqs).items()
        first = context[reqs[0][0]]
        prev = None
        for mid, ts, _, _ in reqs:
            facts.append((mid, None if prev is None else ts - prev, first))
            prev = ts
    invalidate(conn, [mid for mid, *_ in facts])
    conn.executemany("INSERT INTO request_cache VALUES (?,?)", events)
    conn.executemany("INSERT INTO request_stream VALUES (?,?,?)", facts)
    firsts = [r[0] for r in conn.execute(  # each Session's main Stream starts where the gap is NULL
        "SELECT s.first_context FROM request_stream s "
        "LEFT JOIN request_surface a ON a.message_id = s.message_id "
        "WHERE s.gap IS NULL AND a.agent_id IS NULL")]
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (MEDIAN_FIRST_META, repr(
        statistics.median(firsts) if firsts else 0.0)))
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (BARE_SUBAGENT_META, json.dumps(
        _learn_bare_subagent(conn))))


def median_first_context(conn: sqlite3.Connection) -> float | None:
    """Lifetime median context of the first request of every Session's main Stream (stored at
    ingest); None when the store has not stored it yet."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (MEDIAN_FIRST_META,)).fetchone()
    return float(row["value"]) if row else None


@dataclass(frozen=True)
class BareSubagentContext:
    """The learned Bare subagent context: the median first-request context of the leanest
    subagent type, with that type and its run count (for the formula text)."""
    tokens: float
    agent_type: str
    runs: int


def _learn_bare_subagent(conn: sqlite3.Connection) -> list | None:
    """[median, type, runs] of the subagent type with the lowest median first-request context among
    those with BARE_MIN_RUNS+ non-fork runs, over all history; None when no type qualifies."""
    by_type: dict[str, list[int]] = defaultdict(list)
    for x in conn.execute(
            "SELECT f.agent_type AS t, s.first_context AS c FROM request_stream s "
            "JOIN request_surface a ON a.message_id = s.message_id "
            "JOIN (SELECT agent_id, MAX(agent_type) AS agent_type, MAX(is_fork) AS is_fork "
            "      FROM subagent_files GROUP BY agent_id) f ON f.agent_id = a.agent_id "
            "WHERE s.gap IS NULL AND f.is_fork = 0 AND f.agent_type IS NOT NULL "
            "AND f.agent_type != 'fork'"):
        by_type[x["t"]].append(x["c"])
    medians = [(statistics.median(cs), t, len(cs)) for t, cs in by_type.items() if len(cs) >= BARE_MIN_RUNS]
    return list(min(medians)) if medians else None


def bare_subagent_context(conn: sqlite3.Connection) -> BareSubagentContext | None:
    """The stored Bare subagent context (lifetime, all projects); None when no type qualifies or
    the store has not stored it yet."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (BARE_SUBAGENT_META,)).fetchone()
    learned = json.loads(row["value"]) if row else None
    return BareSubagentContext(float(learned[0]), learned[1], int(learned[2])) if learned else None
