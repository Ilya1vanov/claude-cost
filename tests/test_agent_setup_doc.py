"""docs/agent-setup.md only names claude-cost commands and settings that exist."""
import re
import shlex
from pathlib import Path

import pytest

from claude_cost import cli
from claude_cost import config as config_mod

DOC = Path(__file__).resolve().parent.parent / "docs" / "agent-setup.md"
PLACEHOLDER = re.compile(r"<[^<>\s]+>")


def doc_commands() -> list[str]:
    """Every `claude-cost ...` command in an inline code span or a fenced block."""
    text = DOC.read_text()
    spans = re.findall(r"`([^`\n]+)`", text)
    fenced = [line.strip() for block in re.findall(r"```[^\n]*\n(.*?)```", text, re.S)
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
    text = DOC.read_text()
    for name in set(re.findall(r"\bspotlight_[a-z_]+\b|\bupdate_check\b", text)):
        assert name in config_mod.SETTINGS, name
