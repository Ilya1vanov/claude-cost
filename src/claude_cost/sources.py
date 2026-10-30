"""Sources: discovers transcripts under a Data root and knows the on-disk shapes."""
from __future__ import annotations

from pathlib import Path


def main_transcripts(root: Path) -> list[Path]:
    """Main transcripts only: projects/<project-dir>/<sessionUuid>.jsonl.

    Subagent transcripts live deeper and `.jsonl.superseded-*` copies do not end in
    `.jsonl`, so neither matches.
    """
    return sorted(p for p in (root / "projects").glob("*/*.jsonl") if p.is_file())
