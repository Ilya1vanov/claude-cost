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


AUTO_PREFIX = "auto:"


def auto_id(key: str) -> str:
    """The id of an Auto-spotlight: derived from its key, so it needs no stored row."""
    return AUTO_PREFIX + key


def compile_pattern(pattern: str, what: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise SpotlightError(f"{what} {pattern!r} is not a valid regular expression: {e}") from None


@dataclass(frozen=True)
class KeyHit:
    """Where a key was found: the field and a snippet of the text around it."""
    field: str
    snippet: str


SNIPPET_CONTEXT = 30  # characters kept on each side of a match


def key_hits(info: SessionInfo, pattern: re.Pattern[str] | None) -> dict[str, list[KeyHit]]:
    """Every key the pattern finds, with each place it was found, keys in field order
    (title, branch, worktree, prompt).

    Matching ignores case and keys are upper-cased, so `proj-12` in a branch and `PROJ-12` in
    a title are the same key. A match touching the end of a prompt cut at the storage limit is dropped.
    """
    hits: dict[str, list[KeyHit]] = {}
    if pattern is None:
        return hits
    for field_name in FIELDS:
        for text in getattr(info, field_name):
            cut = field_name == "prompt" and len(text) >= PROMPT_LIMIT
            for m in pattern.finditer(text):
                if cut and m.end() >= len(text):
                    continue
                snippet = text[max(0, m.start() - SNIPPET_CONTEXT):m.end() + SNIPPET_CONTEXT].strip()
                hits.setdefault(m.group(0).upper(), []).append(KeyHit(field_name, snippet))
    return hits


def detect_keys(info: SessionInfo, pattern: re.Pattern[str] | None) -> list[str]:
    """Every key the pattern finds, in field order, no repeats."""
    return list(key_hits(info, pattern))


def key_matches(hits: dict[str, list[KeyHit]]) -> set[str]:
    """The keys a Session joins automatically: found in its title, or, when the title carries
    no key, in its branch or worktree path. Any other key it carries is only suggested."""
    in_title = {k for k, hs in hits.items() if any(h.field == "title" for h in hs)}
    if in_title:
        return in_title
    return {k for k, hs in hits.items() if any(h.field in ("branch", "worktree") for h in hs)}


@dataclass(frozen=True)
class Definition:
    id: str                         # stable identity (ADR-0004): generated, or `auto:<KEY>`
    name: str                       # editable label; an Auto-spotlight's is its key until renamed
    kind: str                       # "picked" | "auto"
    key: str = ""                   # the key of an Auto-spotlight
    picked: frozenset[str] = field(default_factory=frozenset)  # logical Session ids
    includes: frozenset[str] = frozenset()  # ids of included Spotlights that exist
    dismissed: frozenset[str] = frozenset()  # Dismissed sessions of an Auto-spotlight (logical Session ids)
    locked: bool = False            # a Locked auto-spotlight: Key matches no longer join by themselves


@dataclass(frozen=True)
class Membership:
    key_matched: frozenset[str]  # Sessions of an Auto-spotlight that carry its key and are not dismissed
    members: frozenset[str]      # Session ids counted in the Spotlight
    related: frozenset[str]      # Forks of members that are not members ("related, excluded")
    via: dict[str, str] = field(default_factory=dict)  # Session -> name of the included Spotlight that brought it
    dismissed: frozenset[str] = frozenset()  # Sessions carrying its key that the user removed


def direct_members(defn: Definition, sessions: dict[str, SessionInfo],
                   keys: dict[str, set[str]]) -> tuple[frozenset[str], frozenset[str]]:
    """(Key matches, direct members) of one Spotlight, ignoring what it includes. `keys` holds
    each Session's Key-match keys. A Dismissed session is out while it carries the key. A Locked
    auto-spotlight has no automatic members: only its Picked sessions."""
    carriers = frozenset(sid for sid in sessions
                         if defn.kind == "auto" and defn.key in keys.get(sid, ()))
    key_matched = frozenset() if defn.locked else carriers - defn.dismissed
    return key_matched, key_matched | {sid for sid in sessions if sid in defn.picked}


def resolve(defn: Definition, sessions: dict[str, SessionInfo], keys: dict[str, set[str]],
            included: Iterable[Definition] = (),
            carried: dict[str, list[str]] | None = None) -> Membership:
    """The Sessions of a Spotlight.

    Direct members: Picked sessions, for an Auto-spotlight its Key matches, and the
    direct members of each Included spotlight (each Session once). A direct member is in,
    whatever it is. Beyond that, Side sessions of a member are in (repeatedly, so Side sessions
    of Side sessions are too). Forks of members that are not direct members are related, excluded.
    """
    key_matched, own = direct_members(defn, sessions, keys)
    carried = keys if carried is None else carried  # every key a Session carries, Key match or not
    dismissed = frozenset(sid for sid in sessions if defn.kind == "auto" and sid in defn.dismissed
                          and defn.key in carried.get(sid, ()))
    via: dict[str, str] = {}
    for inc in sorted(included, key=lambda d: (d.name, d.id)):
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
    return Membership(key_matched, frozenset(members), frozenset(related), via, dismissed)


def match_score(query: str, text: str) -> int | None:
    """How well `query` matches `text` as a case-insensitive substring; None when it doesn't.

    Higher is better: a match that starts earlier scores higher (0 at the start, minus one
    point per position after it).
    """
    at = text.lower().find(query.lower())
    return None if at == -1 else -at
