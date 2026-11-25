from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from claude_cost.releases import Release, github_latest, update_checker

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
NEW = Release("0.2.0", "Faster dashboard.", "https://example.com/claude_cost-0.2.0-py3-none-any.whl")


class FakeReleases:
    def __init__(self, release=NEW, error=None):
        self.release, self.error, self.calls = release, error, 0

    def __call__(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.release


def checker(releases, tmp_path, *, current="0.1.0", now=NOW, enabled=True):
    return update_checker(releases, current=current, cache_path=tmp_path / "update.json",
                          now=lambda: now, enabled=enabled)


def test_github_latest_reads_version_notes_and_wheel_url():
    payload = {"tag_name": "v0.2.0", "body": "Faster dashboard.", "assets": [
        {"name": "notes.txt", "browser_download_url": "https://example.com/notes.txt"},
        {"name": "claude_cost-0.2.0-py3-none-any.whl",
         "browser_download_url": "https://example.com/claude_cost-0.2.0-py3-none-any.whl"}]}
    urls = []

    def fetch(url):
        urls.append(url)
        return json.dumps(payload).encode()

    assert github_latest(fetch=fetch) == NEW
    assert urls == ["https://api.github.com/repos/Ilya1vanov/claude-cost/releases/latest"]


def test_github_latest_without_a_wheel_is_no_release():
    payload = {"tag_name": "v0.2.0", "body": "", "assets": []}
    assert github_latest(fetch=lambda url: json.dumps(payload).encode()) is None


def test_newer_release_is_offered(tmp_path):
    assert checker(FakeReleases(), tmp_path)() == NEW


def test_same_or_older_release_is_not_offered(tmp_path):
    assert checker(FakeReleases(), tmp_path, current="0.2.0")() is None
    assert checker(FakeReleases(), tmp_path, current="0.10.0")() is None


def test_unknown_current_version_offers_nothing(tmp_path):
    assert checker(FakeReleases(), tmp_path, current="unknown")() is None


def test_offline_fails_silently(tmp_path):
    assert checker(FakeReleases(error=OSError("offline")), tmp_path)() is None


def test_lookup_runs_once_a_day_across_calls_and_processes(tmp_path):
    releases = FakeReleases()
    for _ in range(3):
        assert checker(releases, tmp_path, now=NOW + timedelta(hours=3))() == NEW
    assert releases.calls == 1
    checker(releases, tmp_path, now=NOW + timedelta(hours=30))()
    assert releases.calls == 2


def test_failed_lookup_is_not_retried_the_same_day(tmp_path):
    releases = FakeReleases(error=OSError("offline"))
    checker(releases, tmp_path)()
    checker(releases, tmp_path)()
    assert releases.calls == 1


def test_cached_release_stops_being_offered_once_installed(tmp_path):
    checker(FakeReleases(), tmp_path)()
    assert checker(FakeReleases(), tmp_path, current="0.2.0")() is None


def test_disabled_makes_no_lookup(tmp_path):
    releases = FakeReleases()
    assert checker(releases, tmp_path, enabled=False)() is None
    assert releases.calls == 0
