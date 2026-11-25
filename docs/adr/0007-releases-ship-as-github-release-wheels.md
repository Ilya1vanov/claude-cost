# Releases ship as GitHub Release wheels, installed and updated by claude-cost itself

Colleagues, technical and not, install claude-cost with a copy-paste install script (which installs `uv` if missing) and update with `claude-cost update`. Each Release is a `vX.Y.Z` tag on `main` whose wheel CI attaches to a GitHub Release; the script and `update` install that wheel by URL with `uv tool`. Day-to-day work lands on `develop`; `main` moves only by release.

## Considered Options

- **PyPI**: the `claude-cost` name belongs to an unrelated project, and a different distribution name would not match the command.
- **Homebrew tap**: familiar to most users, but it keeps only the latest formula, so a failed update cannot restore the previous version (see ADR-0008). Deferred to #116.
- **Install from git (`git+https://…`)**: needs `git` on the machine, which a fresh Mac lacks, and tracks commits rather than Releases.

## Consequences

- The repo must be public for the wheel URLs and the latest-version check to work.
- The dashboard and CLI check the GitHub API for a newer Release at most once a day, unless the user opts out: the one network call claude-cost makes.
- Releases stay on `0.x` until 1.0 is announced; a breaking change bumps the minor version.
