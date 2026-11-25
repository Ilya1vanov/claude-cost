"""docs/agent-setup.md only names claude-cost commands and settings that exist."""
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from claude_cost import cli
from claude_cost import config as config_mod

DOC = Path(__file__).resolve().parent.parent / "docs" / "agent-setup.md"
PLACEHOLDER = re.compile(r"<[^<>\s]+>")
FENCED = re.compile(r"```[^\n]*\n(.*?)```", re.S)


def settings_path(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def doc_commands() -> list[str]:
    """Every `claude-cost ...` command in an inline code span or a fenced block."""
    text = DOC.read_text()
    spans = re.findall(r"`([^`\n]+)`", text)
    fenced = [line.strip() for block in FENCED.findall(text)
              for line in block.splitlines()]
    return sorted({c for c in spans + fenced if c.startswith("claude-cost ")})


def test_document_names_the_commands_the_setup_runs():
    commands = doc_commands()
    for needed in ("config set spotlight_key_pattern", "config get spotlight_key_pattern",
                   "--version", "ingest", "today", "serve"):
        assert any(needed in c for c in commands), needed


@pytest.mark.parametrize("command", doc_commands())
def test_command_is_accepted_by_the_cli(command, env, monkeypatch):
    """Parse with the real argparse parser; handlers are stubbed so nothing runs."""
    seen = {}
    monkeypatch.setattr(cli, "cmd_config", lambda args, *a, **k: seen.update(args=args) or 0)
    monkeypatch.setattr(cli, "_dispatch", lambda args, *a, **k: seen.update(args=args) or 0)
    argv = shlex.split(PLACEHOLDER.sub("x", command))[1:]
    try:
        cli.main(argv, env=env)
    except SystemExit as e:
        if e.code == 0:  # --version
            return
        pytest.fail(f"CLI rejects `{command}`")
    args = seen["args"]
    if args.command == "config" and getattr(args, "key", None):
        assert args.key in config_mod.SETTINGS, f"unknown setting in `{command}`"


def test_every_snake_case_setting_name_exists():
    """Every backticked snake_case token is a setting name, bar GitHub API fields."""
    for name in set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", DOC.read_text())) - {"tag_name"}:
        assert name in config_mod.SETTINGS, name


def fallback_command() -> str:
    """The `uv run --no-project python -c ...` line from the document's fenced blocks."""
    blocks = FENCED.findall(DOC.read_text())
    lines = [ln.strip() for b in blocks for ln in b.splitlines()
             if ln.strip().startswith("uv run --no-project python -c ")]
    assert len(lines) == 1, "the document must show exactly one fallback command"
    return lines[0]


def fallback_body() -> str:
    """The script between the single quotes of the fallback command."""
    prefix = "uv run --no-project python -c '"
    command = fallback_command()
    assert command.startswith(prefix) and command.endswith("'"), command
    return command[len(prefix):-1]


def run_fallback(home: Path) -> Path:
    result = subprocess.run([sys.executable, "-c", fallback_body()], capture_output=True,
                            text=True, env={**os.environ, "HOME": str(home)})
    assert result.returncode == 0, result.stderr
    return settings_path(home)


def write_settings(home: Path, data: dict) -> None:
    (home / ".claude").mkdir(exist_ok=True)
    settings_path(home).write_text(json.dumps(data))


def test_fallback_creates_a_missing_file_with_365(tmp_path):
    assert json.loads(run_fallback(tmp_path).read_text()) == {"cleanupPeriodDays": 365}


def test_fallback_keeps_other_keys(tmp_path):
    write_settings(tmp_path, {"model": "opus", "permissions": {"allow": ["Bash(ls)"]}})
    assert json.loads(run_fallback(tmp_path).read_text()) == {
        "model": "opus", "permissions": {"allow": ["Bash(ls)"]}, "cleanupPeriodDays": 365}


@pytest.mark.parametrize("before", [1, 30, 364])
def test_fallback_raises_a_shorter_retention(tmp_path, before):
    write_settings(tmp_path, {"cleanupPeriodDays": before})
    assert json.loads(run_fallback(tmp_path).read_text())["cleanupPeriodDays"] == 365


@pytest.mark.parametrize("before", [365, 400, 3650])
def test_fallback_keeps_a_longer_retention(tmp_path, before):
    write_settings(tmp_path, {"cleanupPeriodDays": before})
    assert json.loads(run_fallback(tmp_path).read_text())["cleanupPeriodDays"] == before


def test_fallback_leaves_zero_alone(tmp_path):
    write_settings(tmp_path, {"cleanupPeriodDays": 0, "model": "opus"})
    assert json.loads(run_fallback(tmp_path).read_text()) == {"cleanupPeriodDays": 0, "model": "opus"}


@pytest.mark.parametrize("before", [False, True, "30", None])
def test_fallback_treats_a_non_number_as_missing(tmp_path, before):
    write_settings(tmp_path, {"cleanupPeriodDays": before})
    assert json.loads(run_fallback(tmp_path).read_text())["cleanupPeriodDays"] == 365
