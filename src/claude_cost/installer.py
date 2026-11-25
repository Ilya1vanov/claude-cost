"""Installer and Dashboard: the injected collaborators that change the machine (ADR-0007, ADR-0008).

`update`, rollback and `uninstall` take an `Installer` and a `Dashboard` so tests record calls and never
run `uv`, replace the real tool or kill a real server.

An Installer has `is_tool_install() -> bool`, `install(wheel_url)`, `uninstall()` and `migrate(env)`;
each of the last three raises `InstallError` with a plain-language reason. A Dashboard has `stop(port)`
and `start()`.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Protocol

TOOL_NAME = "claude-cost"


class InstallError(Exception):
    """An install, uninstall or migrate step failed; the message says why."""


class Installer(Protocol):
    def is_tool_install(self) -> bool: ...
    def install(self, wheel_url: str) -> None: ...
    def uninstall(self) -> None: ...
    def migrate(self, env: Mapping[str, str]) -> None: ...


def _run(cmd: list[str], env: Mapping[str, str] | None = None) -> str:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, env=env)
    except OSError as e:
        raise InstallError(f"could not run {cmd[0]}: {e}") from None
    if done.returncode != 0:
        raise InstallError((done.stderr or done.stdout).strip() or f"{cmd[0]} exited with {done.returncode}")
    return done.stdout.strip()


class UvToolInstaller:
    """The real Installer: `uv tool install --force <wheel URL>` and `uv tool uninstall claude-cost`."""

    def is_tool_install(self) -> bool:
        """True when this process runs from uv's tool environment for claude-cost, not a checkout."""
        try:
            tool_dir = Path(_run(["uv", "tool", "dir"])).resolve()
        except InstallError:
            return False
        return (tool_dir / TOOL_NAME).resolve() == Path(sys.prefix).resolve()

    def install(self, wheel_url: str) -> None:
        _run(["uv", "tool", "install", "--force", wheel_url])

    def uninstall(self) -> None:
        _run(["uv", "tool", "uninstall", TOOL_NAME])

    def migrate(self, env: Mapping[str, str]) -> None:
        """Run the installed (new) version's `claude-cost migrate` as a subprocess."""
        command = Path(_run(["uv", "tool", "dir", "--bin"])) / TOOL_NAME
        _run([str(command), "migrate"], env={**os.environ, **env})


class Dashboard:
    """The real stop/restart of a dashboard started by `claude-cost serve`."""

    def stop(self, port: int) -> None:
        """Terminate whatever listens on 127.0.0.1:`port` (the caller checked it is our dashboard)."""
        try:
            pids = _run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"]).split()
        except InstallError as e:
            raise InstallError(f"could not find the dashboard process: {e}") from None
        for pid in pids:
            os.kill(int(pid), signal.SIGTERM)

    def start(self) -> None:
        """Start the (possibly just reinstalled) dashboard in the background, without a browser tab."""
        command = shutil.which(TOOL_NAME) or TOOL_NAME
        try:
            subprocess.Popen([command, "serve", "--browser", "none"], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            raise InstallError(f"could not restart the dashboard: {e}") from None
