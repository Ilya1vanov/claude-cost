"""Project display names: the one mapping from a project key (the encoded folder name Claude
Code groups transcripts under) to a readable name, from the working directories sessions recorded."""
from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import PurePosixPath


def _split(cwd: str) -> tuple[str, str, str | None]:
    """(name, parent, worktree-display or None) for a working directory."""
    parts = PurePosixPath(cwd).parts
    if "worktrees" in parts[:-1]:
        i = parts.index("worktrees")
        repo_at = i - 2 if i >= 2 and parts[i - 1] == ".claude" else i - 1
        if repo_at >= 0 and i + 1 < len(parts):
            return parts[-1], "", f"{parts[repo_at]} ({parts[i + 1]})"
    return parts[-1] if parts else cwd, parts[-2] if len(parts) > 1 else "", None


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
    plain = Counter(s[0] for s in split.values() if s and s[2] is None)
    out = {}
    for p, s in split.items():
        if s is None:
            out[p] = p
        elif s[2] is not None:
            out[p] = s[2]
        else:
            out[p] = f"{s[1]}/{s[0]}" if plain[s[0]] > 1 and s[1] else s[0]
    return out
