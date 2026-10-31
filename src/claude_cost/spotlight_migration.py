"""One-time step: turn each user Spotlight's regex rule into Picked sessions.

Before Picked sessions, a Spotlight could be defined by a rule (a regex over title, git branch,
worktree path and first prompt). The rule is resolved once, against the Sessions the store holds
when the step runs, and its matches are saved as Picked sessions, so the Spotlight keeps its total
and its Sessions. Then the rule is cleared. Guarded by a `meta` key, like the other one-time
backfills in `ingest`.
"""
from __future__ import annotations

import re
import sqlite3

from .query import QueryService

GUARD = "spotlight_picked"


def run(conn: sqlite3.Connection) -> None:
    """Migrate rules to Picked sessions unless that already happened. Needs Sessions grouped."""
    if conn.execute("SELECT 1 FROM meta WHERE key = ?", (GUARD,)).fetchone():
        return
    rules = conn.execute("SELECT name, rule FROM spotlights WHERE rule != ''").fetchall()
    infos = QueryService(conn)._session_infos() if rules else {}
    with conn:
        for name, rule in rules:
            pattern = re.compile(rule, re.IGNORECASE)
            conn.executemany(
                "INSERT OR IGNORE INTO spotlight_pins VALUES (?, ?)",
                [(name, sid) for sid, info in infos.items()
                 if any(pattern.search(t) for t in info.texts())])
        conn.execute("UPDATE spotlights SET rule = ''")
        conn.execute("INSERT INTO meta VALUES (?, '1')", (GUARD,))
