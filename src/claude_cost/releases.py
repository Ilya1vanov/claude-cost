"""Releases: the latest GitHub Release (version, plain-language notes, wheel URL) and the daily update check.

`Releases` is the injected collaborator: a callable returning the latest `Release`, or None when there
is none. The CLI and web app take one so tests use a fake and never touch the network. The update check
is the only network call claude-cost makes.
"""
from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

REPO = "Ilya1vanov/claude-cost"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_INTERVAL = timedelta(days=1)
TIMEOUT_SECONDS = 3


@dataclass(frozen=True)
class Release:
    version: str  # "0.2.0", without the tag's "v"
    notes: str  # the Release body: the changelog entry, in plain language
    wheel_url: str


Releases = Callable[[], "Release | None"]


def _http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "claude-cost"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read()


def github_latest(fetch: Callable[[str], bytes] = _http_get) -> Release | None:
    """The latest GitHub Release, or None when it has no wheel. Network errors propagate."""
    data = json.loads(fetch(LATEST_URL))
    wheel = next((a["browser_download_url"] for a in data.get("assets", [])
                  if a.get("name", "").endswith(".whl")), None)
    if not wheel:
        return None
    return Release(str(data["tag_name"]).removeprefix("v"), data.get("body") or "", wheel)


def wheel_url_for(version: str) -> str:
    """Where a published Release's wheel lives, for reinstalling a version we hold no Release object for."""
    return f"https://github.com/{REPO}/releases/download/{version}/claude_cost-{version}-py3-none-any.whl"


def parse_version(text: str) -> tuple[int, ...] | None:
    """`0.10.2` or `v0.10.2` as a tuple; None when it is not a plain dotted number."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(p) for p in match.group(1).split(".")) if match else None


def is_newer(release: Release, current: str) -> bool:
    new, old = parse_version(release.version), parse_version(current)
    return new is not None and old is not None and new > old


def _read_cache(path: Path, now: datetime) -> tuple[bool, Release | None]:
    """(fresh, release): whether a lookup happened within the interval, and what it found."""
    try:
        cached = json.loads(path.read_text())
        checked = datetime.fromisoformat(cached["checked_at"])
        release = Release(**cached["release"]) if cached.get("release") else None
    except (OSError, ValueError, KeyError, TypeError):
        return False, None
    return now - checked < CHECK_INTERVAL, release


def _write_cache(path: Path, now: datetime, release: Release | None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checked_at": now.isoformat(),
                                    "release": asdict(release) if release else None}))
    except OSError:
        pass


def update_checker(releases: Releases, *, current: str, cache_path: Path,
                   now: Callable[[], datetime], enabled: bool = True) -> Callable[[], Release | None]:
    """A callable giving the Release newer than `current`, or None.

    Asks `releases` at most once a day (the answer, including a failed lookup, is cached in
    `cache_path`), never when `enabled` is off, and never raises.
    """
    def available() -> Release | None:
        if not enabled:
            return None
        moment = now()
        fresh, release = _read_cache(cache_path, moment)
        if not fresh:
            try:
                release = releases()
            except Exception:  # offline, rate limited, bad payload: the check must never break a command
                release = None
            _write_cache(cache_path, moment, release)
        return release if release and is_newer(release, current) else None
    return available
