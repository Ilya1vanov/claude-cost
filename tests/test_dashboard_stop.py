from __future__ import annotations

import pytest

from claude_cost import installer
from claude_cost.installer import Dashboard, InstallError


def stub(monkeypatch, listening):
    """`lsof` finds pid 42, killing it is recorded, and `listening` answers the port probe."""
    kills, answers = [], iter(listening)
    monkeypatch.setattr(installer, "_run", lambda cmd, env=None: "42")
    monkeypatch.setattr(installer.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(installer, "_listening", lambda port: next(answers, listening[-1]))
    return kills


def test_stop_waits_until_the_port_is_free(monkeypatch):
    probes = [True, True, False]
    kills = stub(monkeypatch, probes)
    Dashboard(timeout=5, interval=0).stop(8765)
    assert kills == [(42, installer.signal.SIGTERM)]


def test_stop_raises_when_the_server_keeps_answering(monkeypatch):
    stub(monkeypatch, [True])
    with pytest.raises(InstallError, match="did not stop"):
        Dashboard(timeout=0.05, interval=0.01).stop(8765)
