"""`claude-cost update` end to end with real wheels, real uv tool dirs and a local fake Release.

Slow (builds two wheels, installs with uv); not gated, like the uv tool install test. Never touches the
user's tools, store or config: everything lives under tmp_path.
"""
import os
import shutil
import sqlite3
import subprocess
import tomllib
from pathlib import Path

import pytest

from claude_cost.releases import wheel_url_for

REAL_HOME = os.environ.get("HOME", "")  # captured before the autouse fixture swaps it
REPO = Path(__file__).resolve().parent.parent
OLD_VERSION = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]  # the repo as it stands
NEW_VERSION = "99.0.0"  # the fake newer Release, always ahead of the repo

NEW_STEP = "def _v2(conn):\n    conn.execute('CREATE TABLE update_e2e (x)')\n\n\n"
FAILING_STEP = "def _v2(conn):\n    conn.execute('CREATE TABLE update_e2e (x)')\n    raise RuntimeError('boom')\n\n\n"

# Runs inside the installed tool's Python so the real UvToolInstaller sees a tool install. The fake Release
# points at the new local wheel; the "published" old wheel URL is mapped to the local old wheel.
DRIVER = """
import sys
from claude_cost import cli, installer, releases
new_wheel, old_wheel, old_url = sys.argv[1:4]
class Local(installer.UvToolInstaller):
    def install(self, wheel_url):
        super().install(old_wheel if wheel_url == old_url else wheel_url)
release = releases.Release(sys.argv[4], "Test release.", new_wheel)
sys.exit(cli.main(["update", "--yes"], releases=lambda: release, installer=Local(),
                  probe_server=lambda url: None))
"""

UNINSTALL_DRIVER = """
import sys
from claude_cost import cli
sys.exit(cli.main(["uninstall"], probe_server=lambda url: None, ask=lambda prompt: "n"))
"""


def _build_wheel(project: Path, out: Path, env) -> Path:
    subprocess.run(["uv", "build", "--wheel", "--no-config", "--out-dir", str(out), str(project)],
                   env=env, check=True, capture_output=True, text=True)
    return next(out.glob("*.whl"))


def _build_new_wheel(tmp_path: Path, env, step: str) -> Path:
    """A copy of the repo at NEW_VERSION with one extra store migration (`step`)."""
    src = tmp_path / "new-src"
    shutil.copytree(REPO / "src", src / "src", ignore=shutil.ignore_patterns("__pycache__"))
    pyproject = (REPO / "pyproject.toml").read_text().replace(f'version = "{OLD_VERSION}"', f'version = "{NEW_VERSION}"')
    assert f'version = "{NEW_VERSION}"' in pyproject
    (src / "pyproject.toml").write_text(pyproject)
    store = src / "src" / "claude_cost" / "store.py"
    text = store.read_text().replace("MIGRATIONS = [_to_v1]", step + "MIGRATIONS = [_to_v1, _v2]")
    assert "_v2" in text
    store.write_text(text)
    return _build_wheel(src, tmp_path / "new", env)


@pytest.fixture
def machine(tmp_path):
    cache = subprocess.run(["uv", "cache", "dir"], env={**os.environ, "HOME": REAL_HOME},
                           capture_output=True, text=True).stdout.strip()
    env = {**os.environ, "UV_CACHE_DIR": cache, "UV_TOOL_DIR": str(tmp_path / "tools"),
           "UV_TOOL_BIN_DIR": str(tmp_path / "bin"), "HOME": str(tmp_path / "home"),
           "CLAUDE_COST_CONFIG_DIR": str(tmp_path / "config"), "CLAUDE_COST_DATA_DIR": str(tmp_path / "data")}
    (tmp_path / "home").mkdir(exist_ok=True)
    old_wheel = _build_wheel(REPO, tmp_path / "old", env)
    subprocess.run(["uv", "tool", "install", "--no-config", str(old_wheel)], env=env, check=True,
                   capture_output=True, text=True)
    subprocess.run([str(tmp_path / "bin" / "claude-cost"), "migrate"], env=env, check=True,
                   capture_output=True, text=True)  # creates the store at the old version's schema
    return env, tmp_path, old_wheel


def _installed_version(env, tmp_path) -> str:
    return subprocess.run([str(tmp_path / "bin" / "claude-cost"), "--version"], env=env, check=True,
                          capture_output=True, text=True).stdout.split()[-1]


def _update(env, tmp_path, old_wheel, step):
    new_wheel = _build_new_wheel(tmp_path, env, step)
    python = tmp_path / "tools" / "claude-cost" / "bin" / "python"
    return subprocess.run([str(python), "-c", DRIVER, str(new_wheel), str(old_wheel), wheel_url_for(OLD_VERSION), NEW_VERSION],
                          env=env, capture_output=True, text=True)


def test_update_installs_the_new_wheel_and_migrates_the_store(machine):
    env, tmp_path, old_wheel = machine
    done = _update(env, tmp_path, old_wheel, NEW_STEP)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _installed_version(env, tmp_path) == NEW_VERSION
    conn = sqlite3.connect(tmp_path / "data" / "store.sqlite")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.close()


def test_failing_migration_restores_the_old_version_and_the_store(machine):
    env, tmp_path, old_wheel = machine
    store = tmp_path / "data" / "store.sqlite"
    before = store.read_bytes()
    done = _update(env, tmp_path, old_wheel, FAILING_STEP)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "left unchanged" in done.stderr
    assert _installed_version(env, tmp_path) == OLD_VERSION
    assert store.read_bytes() == before


def test_uninstall_removes_the_command_and_keeps_the_data(machine):
    env, tmp_path, _ = machine
    command = tmp_path / "bin" / "claude-cost"
    python = tmp_path / "tools" / "claude-cost" / "bin" / "python"
    assert command.exists()
    done = subprocess.run([str(python), "-c", UNINSTALL_DRIVER], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not command.exists()
    assert not (tmp_path / "tools" / "claude-cost").exists()
    assert (tmp_path / "data" / "store.sqlite").exists()
