"""Ownership (ADR-0001): every Request is counted once, by message id.

Owner = the transcript whose filename stem equals the record's `sessionId`; if no
candidate matches, the transcript that started earliest.
"""
from __future__ import annotations

from typing import Sequence


def pick_owner(candidates: Sequence[dict]) -> dict:
    """Each candidate: {"file_stem", "session_id", "file_first_ts"}."""
    matching = [c for c in candidates if c["file_stem"] == c["session_id"]]
    pool = matching or list(candidates)
    return min(pool, key=lambda c: c["file_first_ts"])
