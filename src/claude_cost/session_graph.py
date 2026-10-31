"""Session graph: which Session a Transcript belongs to, and each Session's Parent session.

Read-only, loaded from the store in a fixed number of queries. A Desktop link (Side session or
Fork) wins over a Fork found from copied history. A Session never links to itself, has at most
one Parent session, and Fork subagent transcripts never produce a link. A link is kept even when
the Parent session has no requests in the store.
"""
from __future__ import annotations

import sqlite3


class SessionGraph:
    def __init__(self, logical: dict[str, str], links: dict[str, tuple[str, str]]):
        self._logical = logical
        self._links = links
        self._members: dict[str, list[str]] = {}
        for stem, sid in sorted(logical.items()):
            self._members.setdefault(sid, []).append(stem)
        self._children: dict[str, list[str]] = {}
        for child, (parent, _) in sorted(links.items()):
            self._children.setdefault(parent, []).append(child)

    def session_of(self, transcript: str) -> str:
        return self._logical.get(transcript, transcript)

    def transcripts(self, session_id: str) -> list[str]:
        return list(self._members.get(session_id, [session_id]))

    def link(self, session_id: str) -> tuple[str, str] | None:
        """(Parent session, "side" | "fork"), or None for a top-level Session."""
        return self._links.get(session_id)

    def children(self, session_id: str) -> list[str]:
        return list(self._children.get(session_id, []))


def load(conn: sqlite3.Connection) -> SessionGraph:
    logical = {r["stem"]: r["logical_session_id"]
               for r in conn.execute("SELECT stem, logical_session_id FROM session_links")}

    def of(transcript: str) -> str:
        return logical.get(transcript, transcript)

    links: dict[str, tuple[str, str]] = {}
    for r in conn.execute("SELECT child_stem, parent_stem, kind FROM session_relations "
                          "ORDER BY child_stem"):
        child, parent = of(r["child_stem"]), of(r["parent_stem"])
        if child != parent:
            links.setdefault(child, (parent, r["kind"]))
    # A transcript holding copied history (forkedFrom) is a Fork of the Session it copied; when
    # it also holds its grandparent's copies, the newest copied record names the immediate parent.
    for r in conn.execute(
            "SELECT f.file_stem AS stem, c.session_id AS orig, MAX(c.ts_epoch) AS last "
            "FROM request_copies c JOIN ingest_files f ON f.path = c.path "
            "WHERE c.is_copy = 1 AND c.path NOT IN (SELECT path FROM subagent_files) "
            "GROUP BY stem, orig ORDER BY stem, last DESC, orig"):
        child, parent = of(r["stem"]), of(r["orig"])
        if child != parent:
            links.setdefault(child, (parent, "fork"))
    return SessionGraph(logical, links)
