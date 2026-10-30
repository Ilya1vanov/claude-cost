"""Sources: discovers transcripts under a Data root and knows the on-disk shapes."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


def main_transcripts(root: Path) -> list[Path]:
    """Main transcripts only: projects/<project-dir>/<sessionUuid>.jsonl.

    Subagent transcripts live deeper and `.jsonl.superseded-*` copies do not end in
    `.jsonl`, so neither matches.
    """
    return sorted(p for p in (root / "projects").glob("*/*.jsonl") if p.is_file())


@dataclass(frozen=True)
class SubagentFile:
    path: Path
    project: str
    session_stem: str  # the parent Session's transcript stem (directory two levels up)
    agent_id: str
    meta: dict  # contents of the sibling agent-<id>.meta.json; {} when absent or unreadable


def subagent_transcripts(root: Path) -> list[SubagentFile]:
    """projects/<project>/<sessionUuid>/subagents/agent-<id>.jsonl with its sibling metadata.

    `<uuid>.superseded-<ms>/` directories are stale copies and are skipped.
    """
    out = []
    for p in sorted((root / "projects").glob("*/*/subagents/agent-*.jsonl")):
        session_dir = p.parent.parent
        if not p.is_file() or ".superseded" in session_dir.name:
            continue
        agent_id = p.stem[len("agent-"):]
        meta: dict = {}
        try:
            loaded = json.loads(p.with_name(f"agent-{agent_id}.meta.json").read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, ValueError):
            pass
        out.append(SubagentFile(p, session_dir.parent.name, session_dir.name, agent_id, meta))
    return out


def desktop_sessions(store_dir: Path) -> list[dict]:
    """Desktop session store records (`local_<uuid>.json`) under `store_dir`, at any depth.

    Unreadable or non-object files (and `deleted_*` markers) are skipped.
    """
    out = []
    for p in sorted(store_dir.rglob("local_*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and isinstance(rec.get("sessionId"), str):
            out.append(rec)
    return out
