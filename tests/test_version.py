from __future__ import annotations

from importlib import metadata

import pytest

from claude_cost import cli


def test_version_flag_prints_installed_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"claude-cost {metadata.version('claude-cost')}"


def test_version_falls_back_to_unknown_without_metadata(monkeypatch, capsys):
    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(cli.metadata, "version", missing)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == "claude-cost unknown"
