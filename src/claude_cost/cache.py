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

import sqlite3
import statistics

from .sql import SESSION_ID_SQL, SESSION_JOIN, context_tokens

TTL_5M = 5 * 60
TTL_1H = 60 * 60
COLD, WARM, NONE = "cold", "warm", "none"
MEDIAN_FIRST_META = "median_first_context"  # the `meta` row holding the lifetime baseline


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


def classify_all(conn: sqlite3.Connection) -> None:
    """Recompute `request_cache` for every request, grouped by logical Session (a Subagent separately).

    The same walk over each Stream in time order stores the facts findings need from before
    their Period (`request_stream`: each Request's idle gap and its Stream's first-request
    context) and the lifetime median first-request context (`meta`)."""
    sessions: dict[tuple[str, str | None], list[tuple[str, float, int, int]]] = {}
    context: dict[str, int] = {}
    for r in conn.execute(
            "SELECT r.message_id, r.ts_epoch, r.cache_write_5m_tokens AS w5, "
            f"r.cache_write_1h_tokens AS w1, {SESSION_ID_SQL} AS sid, "
            "r.input_tokens AS inp, r.cache_read_tokens AS cr, a.agent_id AS agent "
            f"FROM requests r {SESSION_JOIN} "
            "LEFT JOIN request_surface a ON a.message_id = r.message_id"):
        sessions.setdefault((r["sid"], r["agent"]), []).append(  # a Subagent has its own cache
            (r["message_id"], r["ts_epoch"], r["w5"], r["w1"]))
        context[r["message_id"]] = context_tokens(r["inp"], r["cr"], r["w5"], r["w1"])
    rows, streams, main_firsts = [], [], []
    for (_, agent), reqs in sessions.items():
        reqs.sort(key=lambda r: (r[1], r[0]))
        rows += classify_session(reqs).items()
        first = context[reqs[0][0]]
        if agent is None:  # the Session's main Stream
            main_firsts.append(first)
        prev = None
        for mid, ts, _, _ in reqs:
            streams.append((mid, None if prev is None else ts - prev, first))
            prev = ts
    conn.execute("DELETE FROM request_cache")
    conn.executemany("INSERT INTO request_cache VALUES (?,?)", rows)
    conn.execute("DELETE FROM request_stream")
    conn.executemany("INSERT INTO request_stream VALUES (?,?,?)", streams)
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (MEDIAN_FIRST_META, repr(
        statistics.median(main_firsts) if main_firsts else 0.0)))


def median_first_context(conn: sqlite3.Connection) -> float:
    """Lifetime median context of the first request of every Session's main Stream (stored at ingest)."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (MEDIAN_FIRST_META,)).fetchone()
    return float(row["value"]) if row else 0.0
