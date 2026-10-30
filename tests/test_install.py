"""uv tool install works and exposes `claude-cost`, using temp tool dirs (never the user's)."""
import os
import subprocess
from pathlib import Path

REAL_HOME = os.environ.get("HOME", "")  # captured before the autouse fixture swaps it
REPO = Path(__file__).resolve().parent.parent


def test_uv_tool_install_exposes_command(tmp_path):
    cache = subprocess.run(["uv", "cache", "dir"], env={**os.environ, "HOME": REAL_HOME}, capture_output=True, text=True).stdout.strip()
    env = {**os.environ, "UV_CACHE_DIR": cache,  # keep the real HOME-based cache reachable
           "UV_TOOL_DIR": str(tmp_path / "tools"), "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
           "HOME": str(tmp_path / "home"),
           "CLAUDE_COST_CONFIG_DIR": str(tmp_path / "config"),
           "CLAUDE_COST_DATA_DIR": str(tmp_path / "data")}
    (tmp_path / "home").mkdir(exist_ok=True)
    subprocess.run(["uv", "tool", "install", "--no-config", str(REPO)], env=env, check=True,
                   capture_output=True, text=True)
    done = subprocess.run([str(tmp_path / "bin" / "claude-cost"), "today"], env=env,
                          capture_output=True, text=True)  # HOME is empty: no ~/.claude, spend 0
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("Today (") and "$0.00" in done.stdout
