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
from typing import Iterable

# The order in which fields are searched for keys; keys are collected from all of them.
FIELDS = ("title", "branch", "worktree", "prompt")

# The first prompt is stored cut to this many characters; a key the cut runs through is untrusted.
PROMPT_LIMIT = 300


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
    for field_name in FIELDS:
        for text in getattr(info, field_name):
            # A match reaching the end of a prompt at the storage limit may have been cut.
            cut = field_name == "prompt" and len(text) >= PROMPT_LIMIT
            for m in pattern.finditer(text):
                if cut and m.end() >= len(text):
                    continue
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
    includes: frozenset[str] = frozenset()  # names of included Spotlights that exist


@dataclass(frozen=True)
class Membership:
    key_matched: frozenset[str]  # Sessions of an Auto-spotlight that carry its key
    members: frozenset[str]      # Session ids counted in the Spotlight
    related: frozenset[str]      # Forks of members that are not members ("related, excluded")
    via: dict[str, str] = field(default_factory=dict)  # Session -> the included Spotlight that brought it


def direct_members(defn: Definition, sessions: dict[str, SessionInfo],
                   keys: dict[str, list[str]]) -> tuple[frozenset[str], frozenset[str]]:
    """(Key matches, direct members) of one Spotlight, ignoring what it includes."""
    key_matched = frozenset(sid for sid in sessions
                            if defn.kind == "auto" and defn.key in keys.get(sid, ()))
    return key_matched, key_matched | {sid for sid in sessions if sid in defn.picked}


def resolve(defn: Definition, sessions: dict[str, SessionInfo], keys: dict[str, list[str]],
            included: Iterable[Definition] = ()) -> Membership:
    """The Sessions of a Spotlight.

    Direct members: Picked sessions, for an Auto-spotlight Sessions carrying the key, and the
    direct members of each Included spotlight (each Session once). A direct member is in,
    whatever it is. Beyond that, Side sessions of a member are in (repeatedly, so Side sessions
    of Side sessions are too). Forks of members that are not direct members are related, excluded.
    """
    key_matched, own = direct_members(defn, sessions, keys)
    via: dict[str, str] = {}
    for inc in sorted(included, key=lambda d: d.name):
        for sid in direct_members(inc, sessions, keys)[1] - own:
            via.setdefault(sid, inc.name)
    direct = own | via.keys()
    members = set(direct)
    while True:
        grown = {sid for sid, info in sessions.items()
                 if sid not in members and info.kind == "side" and info.parent in members}
        if not grown:
            break
        members |= grown
    related = {sid for sid, info in sessions.items()
               if sid not in members and info.kind == "fork" and info.parent in members}
    return Membership(key_matched, frozenset(members), frozenset(related), via)


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
