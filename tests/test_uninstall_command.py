from __future__ import annotations

import io
from datetime import datetime, timezone

from claude_cost import cli
from claude_cost.config import load_config
from claude_cost.web import HEALTH
from test_update_command import FakeDashboard, FakeInstaller, make_store

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def run(argv, env, *, tool=True, running=False, answer="n", **installer_kw):
    log, asked = [], []
    installer = FakeInstaller(tool=tool, log=log, **installer_kw)

    def ask(prompt):
        asked.append(prompt)
        return answer
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, env=env, now=NOW, out=out, err=err, installer=installer,
                    dashboard=FakeDashboard(log), ask=ask,
                    probe_server=lambda url: HEALTH if running else None)
    return code, out.getvalue(), err.getvalue(), log, asked


def data_files(env):
    """Create the store, a backup, the update-check cache; return them with the config file."""
    config = load_config(env)
    make_store(env)
    backup = config.store_path.with_name("store.sqlite.0.1.0.bak")
    cache = config.store_path.parent / "update-check.json"
    backup.write_text("b")
    cache.write_text("{}")
    return config, [config.store_path, backup, cache, config.config_path]


def test_uninstall_stops_dashboard_then_removes_tool(env):
    make_store(env)
    code, out, err, log, _ = run(["uninstall"], env, running=True)
    assert code == 0
    assert log == [("stop", 8765), ("uninstall",)]


def test_uninstall_without_running_dashboard_does_not_stop(env):
    code, *_, log, _ = run(["uninstall"], env)
    assert code == 0 and log == [("uninstall",)]


def test_default_answer_keeps_data_and_prints_locations(env):
    config, files = data_files(env)
    code, out, err, log, asked = run(["uninstall"], env, answer="")
    assert code == 0
    assert asked == ["Also delete your data (Spotlights, settings)? [y/N] "]
    assert all(f.exists() for f in files)
    assert str(config.store_path.parent) in out and str(config.config_path) in out


def test_yes_answer_deletes_data(env):
    _, files = data_files(env)
    code, out, *_ = run(["uninstall"], env, answer="y")
    assert code == 0
    assert not any(f.exists() for f in files)


def test_purge_deletes_without_asking(env):
    _, files = data_files(env)
    code, out, err, log, asked = run(["uninstall", "--purge"], env)
    assert code == 0 and asked == []
    assert not any(f.exists() for f in files)


def test_never_touches_uv(env):
    *_, log, _ = run(["uninstall", "--purge"], env)
    assert log == [("uninstall",)]  # only the tool: no uv removal step exists


def test_refuses_outside_a_uv_tool_install(env):
    _, files = data_files(env)
    code, out, err, log, asked = run(["uninstall", "--purge"], env, tool=False)
    assert code == 1 and "git pull" not in err and "uv tool" in err
    assert log == [] and asked == [] and all(f.exists() for f in files)


def test_failed_removal_keeps_data(env):
    _, files = data_files(env)
    installer_fail = FakeInstaller(log=[])
    installer_fail.uninstall = lambda: (_ for _ in ()).throw(cli.InstallError("busy"))
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["uninstall", "--purge"], env=env, now=NOW, out=out, err=err, installer=installer_fail,
                    dashboard=FakeDashboard([]), ask=lambda p: "y", probe_server=lambda u: None)
    assert code == 1 and "busy" in err.getvalue()
    assert all(f.exists() for f in files)
