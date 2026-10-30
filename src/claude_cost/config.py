"""Config: where Data roots, the store and the config file live.

Everything is resolved from an explicit environment mapping so tests never touch
the real home directory.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

DEFAULT_PORT = 8765


@dataclass(frozen=True)
class Config:
    data_roots: list[Path]
    store_path: Path
    config_path: Path
    timezone: str | None = None  # IANA name; None = system local time
    port: int = DEFAULT_PORT


def load_config(env: Mapping[str, str]) -> Config:
    home = Path(env.get("HOME", str(Path.home())))
    config_dir = Path(env.get("CLAUDE_COST_CONFIG_DIR") or home / ".config" / "claude-cost")
    data_dir = Path(env.get("CLAUDE_COST_DATA_DIR") or home / ".local" / "share" / "claude-cost")
    config_path = config_dir / "config.toml"
    raw: dict = {}
    if config_path.exists():
        raw = tomllib.loads(config_path.read_text())
    roots = [Path(p).expanduser() for p in raw.get("data_roots", [])] or [home / ".claude"]
    return Config(
        data_roots=roots,
        store_path=data_dir / "store.sqlite",
        config_path=config_path,
        timezone=raw.get("timezone"),
        port=int(raw.get("port", DEFAULT_PORT)),
    )
