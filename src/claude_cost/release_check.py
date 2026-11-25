"""Verify a release tag against pyproject.toml and CHANGELOG.md (ADR-0007).

CLI, used by the release workflow:
    python -m claude_cost.release_check <tag> <pyproject> <changelog> <entry-out>
"""

import re
import sys
import tomllib
from pathlib import Path


class ReleaseCheckError(Exception):
    pass


def check_release(tag: str, version: str, changelog: str) -> str:
    """Return the changelog entry for `tag`, or raise ReleaseCheckError."""
    if tag != f"v{version}":
        raise ReleaseCheckError(
            f"tag {tag} does not match pyproject.toml version {version}"
        )
    heading = re.compile(rf"^## {re.escape(tag)}(\s.*)?$", re.MULTILINE)
    match = heading.search(changelog)
    if match is None:
        raise ReleaseCheckError(f"CHANGELOG.md has no entry for {tag}")
    rest = changelog[match.end():]
    next_heading = re.search(r"^## ", rest, re.MULTILINE)
    entry = (rest[: next_heading.start()] if next_heading else rest).strip()
    if not entry:
        raise ReleaseCheckError(f"CHANGELOG.md entry for {tag} is empty")
    return entry


def main(argv: list[str]) -> int:
    tag, pyproject, changelog, entry_out = argv
    version = tomllib.loads(Path(pyproject).read_text())["project"]["version"]
    try:
        entry = check_release(tag, version, Path(changelog).read_text())
    except ReleaseCheckError as exc:
        print(f"release check failed: {exc}", file=sys.stderr)
        return 1
    Path(entry_out).write_text(entry + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
