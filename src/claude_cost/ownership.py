"""Ownership (ADR-0001): every Request is counted once, by message id.

Owner = the transcript whose filename stem equals the record's `sessionId`; if no
candidate matches, the transcript that started earliest.
"""
from __future__ import annotations

from typing import Sequence


def pick_owner(candidates: Sequence[dict]) -> dict:
    """Each candidate: {"file_stem", "session_id", "file_first_ts"} plus optional flags.

    `is_copy` marks a record that its transcript flags as copied history (a Fork's
    `forkedFrom`); `relocated` marks a transcript that carries a `relocated` record (a
    worktree-move copy). Both lose to an unflagged candidate, so the original transcript
    keeps the request. Ties are broken deterministically.
    """
    def key(c):
        return (bool(c.get("is_copy")), bool(c.get("relocated")), c["file_first_ts"],
                c.get("project", ""), c["file_stem"])

    matching = [c for c in candidates if c["file_stem"] == c["session_id"]]
    return min(matching or list(candidates), key=key)
