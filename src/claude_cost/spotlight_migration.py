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

from . import spotlights, store
from .query import QueryService

GUARD = "spotlight_picked"
KEY_GUARD = "spotlight_key_named"


def run(conn: sqlite3.Connection) -> None:
    """Migrate rules to Picked sessions unless that already happened. Needs Sessions grouped."""
    if conn.execute("SELECT 1 FROM meta WHERE key = ?", (GUARD,)).fetchone():
        return
    rules = conn.execute("SELECT id, rule FROM spotlights WHERE rule != ''").fetchall()
    infos = QueryService(conn)._session_infos() if rules else {}
    with conn:
        done = []
        for sp_id, rule in rules:
            try:
                pattern = re.compile(rule, re.IGNORECASE)
            except re.error:
                continue  # a rule that no longer compiles is left as stored, never breaking ingest
            conn.executemany(
                "INSERT OR IGNORE INTO spotlight_pins VALUES (?, ?)",
                [(sp_id, sid) for sid, info in infos.items()
                 if any(pattern.search(t) for t in info.texts())])
            done.append(sp_id)
        conn.executemany("UPDATE spotlights SET rule = '' WHERE id = ?", [(n,) for n in done])
        conn.execute("INSERT INTO meta VALUES (?, '1')", (GUARD,))


def lock_key_named(conn: sqlite3.Connection, key_pattern: str | None) -> None:
    """Turn each user-defined Spotlight named like a key (the whole name matches the key pattern)
    into that key's Locked auto-spotlight, keeping its name, picks and the edges that include it.
    Runs once, the first time a key pattern is configured. A Spotlight that includes others cannot
    become an Auto-spotlight, and of several whose names differ only by case (one key) only the
    first does: those stay user-defined. The rest are converted and committed, the step is marked
    done, and then SpotlightError names the ones that cannot (raised once, never on later ingests)."""
    if not key_pattern or conn.execute("SELECT 1 FROM meta WHERE key = ?", (KEY_GUARD,)).fetchone():
        return
    pattern = spotlights.compile_pattern(key_pattern, "spotlight_key_pattern")
    named = [r for r in conn.execute("SELECT id, name FROM spotlights ORDER BY name, id")
             if pattern.fullmatch(r["name"])]
    includers = {r["id"] for r in named
                 if conn.execute("SELECT 1 FROM spotlight_includes WHERE spotlight = ?", (r["id"],)).fetchone()}
    taken: set[str] = set()
    with conn:  # also commits what the ingest wrote before this step
        for r in named:
            key = r["name"].upper()
            if r["id"] in includers or key in taken:
                continue
            taken.add(key)
            auto = spotlights.auto_id(key)
            store.set_auto_state(conn, key, name=r["name"], locked=True)
            conn.execute("UPDATE OR IGNORE spotlight_pins SET spotlight = ? WHERE spotlight = ?", (auto, r["id"]))
            conn.execute("UPDATE OR IGNORE spotlight_includes SET included = ? WHERE included = ?", (auto, r["id"]))
            conn.execute("DELETE FROM spotlight_pins WHERE spotlight = ?", (r["id"],))
            conn.execute("DELETE FROM spotlight_includes WHERE included = ?", (r["id"],))
            conn.execute("DELETE FROM spotlights WHERE id = ?", (r["id"],))
        conn.execute("INSERT INTO meta VALUES (?, '1')", (KEY_GUARD,))
    if includers:
        names = ", ".join(repr(r["name"]) for r in named if r["id"] in includers)
        raise spotlights.SpotlightError(
            f"the Spotlight(s) {names} are named like a key but include other Spotlights, so they stay "
            "user-defined; remove their includes and lock the key's Auto-spotlight by hand")
