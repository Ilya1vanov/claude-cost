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

TTL_5M = 5 * 60
TTL_1H = 60 * 60
COLD, WARM, NONE = "cold", "warm", "none"


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
    """Recompute `request_cache` for every request, grouped by logical Session."""
    sessions: dict[str, list[tuple[str, float, int, int]]] = {}
    for r in conn.execute(
            "SELECT r.message_id, r.ts_epoch, r.cache_write_5m_tokens AS w5, "
            "r.cache_write_1h_tokens AS w1, COALESCE(l.logical_session_id, r.session_id) AS sid "
            "FROM requests r LEFT JOIN session_links l ON l.stem = r.session_id"):
        sessions.setdefault(r["sid"], []).append(
            (r["message_id"], r["ts_epoch"], r["w5"], r["w1"]))
    rows = [(mid, ev) for reqs in sessions.values()
            for mid, ev in classify_session(reqs).items()]
    conn.execute("DELETE FROM request_cache")
    conn.executemany("INSERT INTO request_cache VALUES (?,?)", rows)
