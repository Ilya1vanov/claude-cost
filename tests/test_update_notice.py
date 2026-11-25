from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_cost import cli, store
from claude_cost.query import QueryService
from claude_cost.releases import Release, update_checker
from claude_cost.web import create_app

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
NEW = Release("0.2.0", "Faster dashboard.", "https://example.com/claude_cost-0.2.0-py3-none-any.whl")
NOTICE = "Update available: run `claude-cost update`"


class FakeReleases:
    def __init__(self, release=NEW, error=None):
        self.release, self.error, self.calls = release, error, 0

    def __call__(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.release


@pytest.fixture(autouse=True)
def installed_version(monkeypatch):
    monkeypatch.setattr(cli, "_version", lambda: "0.1.0")


def run(argv, env, releases):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, env=env, now=NOW, out=out, err=err, releases=releases)
    return code, out.getvalue(), err.getvalue()


def test_cli_command_prints_one_line_notice_for_a_newer_release(configure, root):
    code, out, err = run(["today"], configure(root), FakeReleases())
    assert code == 0
    assert err.splitlines().count(NOTICE) == 1
    assert NOTICE not in out


def test_no_notice_without_a_newer_release(configure, root):
    for releases in (FakeReleases(release=None), FakeReleases(release=Release("0.1.0", "", "u")),
                     FakeReleases(error=OSError("offline"))):
        code, out, err = run(["today"], configure(root), releases)
        assert code == 0 and NOTICE not in out + err


def test_lookup_runs_once_a_day_across_commands(configure, root):
    releases = FakeReleases()
    env = configure(root)
    for _ in range(3):
        run(["today"], env, releases)
    assert releases.calls == 1


def test_json_output_never_carries_the_notice(configure, root):
    releases = FakeReleases()
    code, out, err = run(["spotlight", "list", "--json"], configure(root), releases)
    assert code == 0 and NOTICE not in out + err
    assert releases.calls == 0


def test_config_key_stops_the_lookup(configure, root):
    env = configure(root)
    config = Path(env["CLAUDE_COST_CONFIG_DIR"]) / "config.toml"
    config.write_text(config.read_text() + "update_check = false\n")
    releases = FakeReleases()
    code, out, err = run(["today"], env, releases)
    assert code == 0 and NOTICE not in out + err
    assert releases.calls == 0


@pytest.mark.parametrize("value", ["0", "false", "off", "no"])
def test_environment_variable_stops_the_lookup(configure, root, value):
    env = {**configure(root), "CLAUDE_COST_UPDATE_CHECK": value}
    releases = FakeReleases()
    code, out, err = run(["today"], env, releases)
    assert code == 0 and NOTICE not in out + err
    assert releases.calls == 0


def test_update_check_is_a_validated_setting(configure, root):
    env = configure(root)
    assert run(["config", "set", "update_check", "false"], env, FakeReleases())[1].strip() == \
        "update_check = false"
    assert run(["config", "get", "update_check"], env, FakeReleases())[1].strip() == "false"
    assert run(["config", "set", "update_check", "maybe"], env, FakeReleases())[0] == 2


def dashboard(tmp_path, release, *, enabled=True):
    check = update_checker(FakeReleases(release), current="0.1.0",
                           cache_path=tmp_path / "update.json", now=lambda: NOW, enabled=enabled)
    client = create_app(lambda: QueryService(store.connect(tmp_path / "web.sqlite"), "UTC"),
                        lambda: NOW, update_available=check).test_client()
    return client.get("/").get_data(as_text=True)


def test_dashboard_shows_banner_for_a_newer_release(tmp_path):
    assert NOTICE.replace("`claude-cost update`", "<code>claude-cost update</code>") in dashboard(tmp_path, NEW)


def test_dashboard_shows_no_banner_without_a_newer_release(tmp_path):
    assert "Update available" not in dashboard(tmp_path, None)
    assert "Update available" not in dashboard(tmp_path, NEW, enabled=False)
