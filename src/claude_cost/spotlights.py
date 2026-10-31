"""Spotlights: which Sessions make up a named piece of work (pure logic, no SQL).

A Spotlight is a set of Picked sessions the user chose, or an Auto-spotlight per key that a
configured pattern finds (which can take Picked sessions too). Its members are those Sessions
plus everything that rolls into them: Subagent spend is already part of a Session, and Side
sessions of a member are members too. A Fork joins only when it is picked itself; otherwise it
is listed as related, excluded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# The order in which fields are searched for keys; keys are collected from all of them.
FIELDS = ("title", "branch", "worktree", "prompt")


class SpotlightError(ValueError):
    """An invalid Spotlight name or pattern; the message is safe to show to the user."""


@dataclass(frozen=True)
class SessionInfo:
    """One logical Session as Spotlights see it."""
    session_id: str
    title: tuple[str, ...] = ()     # custom titles, one per transcript that has one
    branch: tuple[str, ...] = ()    # git branch, per transcript
    worktree: tuple[str, ...] = ()  # working directory, per transcript
    prompt: tuple[str, ...] = ()    # first prompt, per transcript
    parent: str | None = None       # parent Session of a Side session or Fork
    kind: str | None = None         # "side" | "fork" | None

    def texts(self) -> list[str]:
        """Searchable text in key order: title, git branch, worktree path, first prompt."""
        return [t for f in FIELDS for t in getattr(self, f)]

    @property
    def label(self) -> str:
        """Short human name: custom title, else the start of the first prompt, else the id."""
        if self.title:
            return self.title[-1]
        if self.prompt:
            return self.prompt[0][:60]
        return self.session_id


def compile_pattern(pattern: str, what: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise SpotlightError(f"{what} {pattern!r} is not a valid regular expression: {e}") from None


def detect_keys(info: SessionInfo, pattern: re.Pattern[str] | None) -> list[str]:
    """Every key the pattern finds, in field order (title, branch, worktree, prompt), no repeats.

    Matching ignores case and keys are upper-cased, so `proj-12` in a branch and `PROJ-12` in
    a title are the same key.
    """
    if pattern is None:
        return []
    keys: list[str] = []
    for text in info.texts():
        for m in pattern.finditer(text):
            key = m.group(0).upper()
            if key not in keys:
                keys.append(key)
    return keys


@dataclass(frozen=True)
class Definition:
    name: str
    kind: str                       # "picked" | "auto"
    key: str = ""                   # the key of an Auto-spotlight
    picked: frozenset[str] = field(default_factory=frozenset)  # logical Session ids


@dataclass(frozen=True)
class Membership:
    key_matched: frozenset[str]  # Sessions of an Auto-spotlight that carry its key
    members: frozenset[str]      # Session ids counted in the Spotlight
    related: frozenset[str]      # Forks of members that are not members ("related, excluded")


def resolve(defn: Definition, sessions: dict[str, SessionInfo],
            keys: dict[str, list[str]]) -> Membership:
    """The Sessions of a Spotlight.

    Direct members: Picked sessions and, for an Auto-spotlight, Sessions carrying the key.
    A direct member is in, whatever it is. Beyond that, Side sessions of a member are in
    (repeatedly, so Side sessions of Side sessions are too). Forks of members that are not
    direct members are related, excluded.
    """
    key_matched = {sid for sid in sessions
                   if defn.kind == "auto" and defn.key in keys.get(sid, ())}
    direct = key_matched | {sid for sid in sessions if sid in defn.picked}
    members = set(direct)
    while True:
        grown = {sid for sid, info in sessions.items()
                 if sid not in members and info.kind == "side" and info.parent in members}
        if not grown:
            break
        members |= grown
    related = {sid for sid, info in sessions.items()
               if sid not in members and info.kind == "fork" and info.parent in members}
    return Membership(frozenset(key_matched), frozenset(members), frozenset(related))


def match_score(query: str, text: str) -> int | None:
    """How well `query` matches `text` as a case-insensitive subsequence; None when it doesn't.

    Higher is better: each pair of query characters that sit next to each other in the text
    adds 10, and a match that starts later loses one point per position. Every place the first
    character occurs is tried as the start, so a contiguous run later in the text beats a
    scattered match that begins earlier.
    """
    q, t = query.lower(), text.lower()
    best: int | None = None
    start = t.find(q[0]) if q else 0
    while start != -1:
        pos, adjacent, ok = start, 0, True
        for i in range(1, len(q)):
            nxt = t.find(q[i], pos + 1)
            if nxt == -1:
                ok = False
                break
            adjacent += nxt == pos + 1
            pos = nxt
        if not ok:
            break  # later starts have even less text left to match in
        score = 10 * adjacent - start
        best = score if best is None else max(best, score)
        start = t.find(q[0], start + 1)
    return best
