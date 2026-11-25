from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest

from claude_cost import cli, store
from claude_cost.installer import Dashboard, InstallError
from claude_cost.releases import Release
from claude_cost.web import HEALTH

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
WHEEL = "https://example.com/claude_cost-0.2.0-py3-none-any.whl"
NEW = Release("0.2.0", "Faster dashboard.", WHEEL)
OLD_WHEEL = "https://github.com/Ilya1vanov/claude-cost/releases/download/0.1.0/claude_cost-0.1.0-py3-none-any.whl"


class FakeInstaller:
    def __init__(self, tool=True, fail_install=None, fail_migrate=False, log=None):
        self.tool, self.fail_install, self.fail_migrate = tool, fail_install, fail_migrate
        self.log = log if log is not None else []

    def is_tool_install(self):
        return self.tool

    def install(self, wheel_url):
        self.log.append(("install", wheel_url))
        if self.fail_install == wheel_url or self.fail_install is True:
            raise InstallError("network down")

    def uninstall(self):
        self.log.append(("uninstall",))

    def migrate(self, env):
        self.log.append(("migrate",))
        if self.fail_migrate:
            raise InstallError("migration exploded")


class FakeDashboard:
    def __init__(self, log):
        self.log = log

    def stop(self, port):
        self.log.append(("stop", port))

    def start(self):
        self.log.append(("start",))


@pytest.fixture(autouse=True)
def installed_version(monkeypatch):
    monkeypatch.setattr(cli, "_version", lambda: "0.1.0")


def run(argv, env, *, release=NEW, tool=True, running=False, answer="y", **installer_kw):
    log = []
    installer = FakeInstaller(tool=tool, log=log, **installer_kw)
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return answer
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, env=env, now=NOW, out=out, err=err, releases=lambda: release,
                    installer=installer, dashboard=FakeDashboard(log), ask=ask,
                    probe_server=lambda url: HEALTH if running else None)
    return code, out.getvalue(), err.getvalue(), log, asked


def make_store(env, version="0.1.0"):
    from claude_cost.config import load_config
    path = load_config(env).store_path
    store.connect(path).close()
    return path


def test_update_yes_runs_every_step_in_order(env):
    path = make_store(env)
    code, out, err, log, asked = run(["update", "--yes"], env, running=True)
    assert code == 0, err
    assert [step[0] for step in log] == ["stop", "install", "migrate", "start"]
    assert log[1] == ("install", WHEEL)
    assert asked == []
    assert path.with_name("store.sqlite.0.1.0.bak").exists()
    assert "Faster dashboard." in out and "0.2.0" in out
    for phrase in ("Stopping the dashboard", "Backing up", "Installing", "Upgrading your data", "Restarting"):
        assert phrase in out
    assert "Updated to 0.2.0" in out


def test_dashboard_not_running_is_neither_stopped_nor_started(env):
    code, out, err, log, _ = run(["update", "--yes"], env, running=False)
    assert code == 0 and [s[0] for s in log] == ["install", "migrate"]


def test_without_yes_shows_notes_and_does_nothing_unless_confirmed(env):
    code, out, err, log, asked = run(["update"], env, answer="n")
    assert code == 0 and log == [] and len(asked) == 1
    assert "Faster dashboard." in out and "nothing was changed" in out.lower()
    code, out, err, log, _ = run(["update"], env, answer="y")
    assert [s[0] for s in log] == ["install", "migrate"]


def test_up_to_date_calls_no_installer(env):
    code, out, err, log, asked = run(["update", "--yes"], env, release=Release("0.1.0", "", WHEEL))
    assert code == 0 and log == [] and asked == []
    assert "up to date" in out


def test_non_tool_install_is_refused_with_guidance(env):
    code, out, err, log, _ = run(["update", "--yes"], env, tool=False)
    assert code == 1 and log == []
    assert "git pull" in err


def test_only_the_most_recent_backup_is_kept(env):
    path = make_store(env)
    (path.parent / "store.sqlite.0.0.9.bak").write_text("older")
    run(["update", "--yes"], env)
    assert sorted(p.name for p in path.parent.glob("*.bak")) == ["store.sqlite.0.1.0.bak"]


def test_failed_migration_reinstalls_previous_release_and_restarts_dashboard(env):
    make_store(env)
    code, out, err, log, _ = run(["update", "--yes"], env, running=True, fail_migrate=True)
    assert code == 1
    assert log == [("stop", 8765), ("install", WHEEL), ("migrate",), ("install", OLD_WHEEL), ("start",)]
    assert "migration exploded" in err and "previous version" in err


def test_failed_install_keeps_old_version_and_restarts_dashboard(env):
    make_store(env)
    code, out, err, log, _ = run(["update", "--yes"], env, running=True, fail_install=WHEEL)
    assert code == 1
    assert log == [("stop", 8765), ("install", WHEEL), ("start",)]
    assert "network down" in err and "nothing changed" in err.lower()


def test_dashboard_collaborator_defaults_exist():
    assert callable(Dashboard().stop) and callable(Dashboard().start)


def test_real_installer_sees_a_dev_checkout_as_not_a_tool_install():
    from claude_cost.installer import UvToolInstaller
    assert UvToolInstaller().is_tool_install() is False
