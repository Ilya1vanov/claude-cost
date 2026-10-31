"""Project display names: the one mapping from a project key (the encoded folder name Claude
Code groups transcripts under) to a readable name, from the working directories sessions recorded."""
from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import PurePosixPath


def _split(cwd: str) -> tuple[tuple[str, ...], str | None]:
    """(path parts, worktree-display or None) for a working directory. A worktree is the
    `<repo>/.claude/worktrees/<name>` layout Claude Code creates, nothing looser."""
    parts = PurePosixPath(cwd).parts
    for i in range(2, len(parts) - 1):
        if parts[i] == "worktrees" and parts[i - 1] == ".claude":
            return parts, f"{parts[i - 2]} ({parts[i + 1]})"
    return parts or (cwd,), None


def project_display_names(conn: sqlite3.Connection) -> dict[str, str]:
    """Project key -> display name: last path component, `parent/name` on a collision,
    `repo (worktree)` for a worktree, the raw key when no working directory was recorded."""
    cwds: dict[str, str] = {}
    for r in conn.execute(
            "SELECT f.project AS p, m.cwd AS cwd FROM ingest_files f "
            "LEFT JOIN session_meta m ON m.stem = f.file_stem ORDER BY f.path"):
        if r["cwd"] and not cwds.get(r["p"]):
            cwds[r["p"]] = r["cwd"]
        else:
            cwds.setdefault(r["p"], "")
    for r in conn.execute("SELECT DISTINCT project FROM requests"):
        cwds.setdefault(r["project"], "")
    split = {p: _split(c) if c else None for p, c in cwds.items()}
    plain = Counter(s[0][-1] for s in split.values() if s and s[1] is None)
    out = {}
    for p, s in split.items():
        if s is None:
            out[p] = p
        elif s[1] is not None:
            out[p] = s[1]
        else:
            out[p] = s[0][-1] if plain[s[0][-1]] < 2 else _unique(p, s[0], split)
    return out


def _unique(key: str, parts: tuple[str, ...], split: dict) -> str:
    """The name with as many parent folders as it takes to tell it from the other plain
    projects of the same name."""
    rivals = [s[0] for p, s in split.items()
              if p != key and s and s[1] is None and s[0][-1] == parts[-1]]
    for depth in range(2, len(parts) + 1):
        label = parts[-depth:]
        if all(r[-depth:] != label for r in rivals):
            break
    return "/".join(label)
