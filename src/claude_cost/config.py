"""Config: where Data roots, the store and the config file live, plus the settings in config.toml.

Everything is resolved from an explicit environment mapping so tests never touch
the real home directory. Settings are declared once in SETTINGS; loading, the
`config` display, `config get/set` validation and the commented default file all derive from it.
"""
from __future__ import annotations

import json
import re
import tomllib
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

DEFAULT_PORT = 8765
DEFAULT_TOP_N = 10


class ConfigError(ValueError):
    """A config value or key is invalid; the message is safe to show to the user."""


@dataclass(frozen=True)
class Config:
    data_roots: list[Path]
    store_path: Path
    config_path: Path
    timezone: str | None = None  # IANA name; None = system local time
    port: int = DEFAULT_PORT
    top_n: int = DEFAULT_TOP_N
    desktop_stores: list[Path] = field(default_factory=list)  # Desktop session store folders
    prices: dict[str, dict[str, float]] = field(default_factory=dict)  # model -> price override
    sources: dict[str, str] = field(default_factory=dict)  # setting -> "default" | "config"


@dataclass(frozen=True)
class Setting:
    name: str
    doc: str
    default: Any  # value in effect when the file does not set it (None = unset)
    default_text: str  # how the default is shown, e.g. '["~/.claude"]'
    example: str  # TOML value used in the commented line of the default file
    coerce: Callable[[Any], Any]  # validates a TOML value, returns the normalised value
    parse: Callable[[str], Any]  # parses a CLI string into a TOML-compatible value


def _int_in(name: str, lo: int, hi: int | None = None) -> Callable[[Any], int]:
    def coerce(v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ConfigError(f"{name} must be an integer, got {v!r}")
        if v < lo or (hi is not None and v > hi):
            rng = f">= {lo}" if hi is None else f"between {lo} and {hi}"
            raise ConfigError(f"{name} must be {rng}, got {v}")
        return v
    return coerce


def _parse_int(name: str) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            return int(text.strip())
        except ValueError:
            raise ConfigError(f"{name} must be an integer, got {text!r}") from None
    return parse


def _coerce_roots(v: Any) -> list[str]:
    if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
        raise ConfigError(f"data_roots must be a non-empty list of paths, got {v!r}")
    return v


def _parse_roots(text: str) -> list[str]:
    return [p.strip() for p in text.split(",") if p.strip()]  # comma-separated on the CLI


def _coerce_stores(v: Any) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise ConfigError(f"desktop_session_stores must be a list of paths, got {v!r}")
    return v


def _coerce_tz(v: Any) -> str:
    if not isinstance(v, str) or not v:
        raise ConfigError(f"timezone must be an IANA time zone name, got {v!r}")
    try:
        zoneinfo.ZoneInfo(v)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        raise ConfigError(f"timezone {v!r} is not a known IANA time zone") from None
    return v


def _coerce_prices(v: Any) -> dict[str, dict[str, float]]:
    if not isinstance(v, dict):
        raise ConfigError(f"prices must be a table of model -> {{input, output, cache_read}}, got {v!r}")
    out: dict[str, dict[str, float]] = {}
    for model, p in v.items():
        if not isinstance(p, dict) or not {"input", "output"} <= set(p) <= {"input", "output", "cache_read"}:
            raise ConfigError(f"prices.{model} needs input and output (and optionally cache_read), got {p!r}")
        for k, x in p.items():
            if isinstance(x, bool) or not isinstance(x, (int, float)) or x < 0:
                raise ConfigError(f"prices.{model}.{k} must be a number >= 0, got {x!r}")
        out[model] = dict(p)
    return out


def _parse_prices(text: str) -> Any:
    raise ConfigError("prices cannot be set from the command line; use `claude-cost config edit`")


SETTINGS: dict[str, Setting] = {s.name: s for s in [
    Setting("data_roots", "Claude config folders to read (Data roots); CLI: comma-separated.",
            None, '["~/.claude"]', '["~/.claude"]', _coerce_roots, _parse_roots),
    Setting("port", "Port for `claude-cost serve` (always bound to 127.0.0.1).",
            DEFAULT_PORT, str(DEFAULT_PORT), str(DEFAULT_PORT),
            _int_in("port", 1, 65535), _parse_int("port")),
    Setting("top_n", "Number of items in the top-N cards.",
            DEFAULT_TOP_N, str(DEFAULT_TOP_N), str(DEFAULT_TOP_N),
            _int_in("top_n", 1), _parse_int("top_n")),
    Setting("desktop_session_stores",
            "Desktop session store folders (scanned recursively for local_*.json) that link Side "
            "sessions, Forks and earlier transcripts; CLI: comma-separated. Empty = no Desktop links.",
            [], "[]", '["~/Library/Application Support/Claude/claude-code-sessions"]',
            _coerce_stores, _parse_roots),
    Setting("timezone", "IANA time zone for days and weeks; unset = system local time.",
            None, "(system local time)", '"Europe/Berlin"', _coerce_tz, str),
    Setting("prices", "Price overrides in $ per million tokens, by model-name prefix "
            "(cache_read defaults to 0.1x input); also adds new models.",
            None, "(none)", '{ "claude-example-1" = { input = 3.0, output = 15.0, cache_read = 0.3 } }',
            _coerce_prices, _parse_prices),
]}


def config_path_for(env: Mapping[str, str]) -> Path:
    home = Path(env.get("HOME", str(Path.home())))
    return Path(env.get("CLAUDE_COST_CONFIG_DIR") or home / ".config" / "claude-cost") / "config.toml"


def default_file_text() -> str:
    parts = ["# claude-cost settings. Uncomment a line to override its default.\n"]
    for s in SETTINGS.values():
        parts.append(f"\n# {s.doc}\n# {s.name} = {s.example}\n")
    return "".join(parts)


def ensure_config_file(path: Path) -> bool:
    """Create the config file with commented defaults on first run. True if created."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(default_file_text())
    return True


def _read_raw(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from None


def _setting(name: str) -> Setting:
    if name not in SETTINGS:
        raise ConfigError(f"unknown config key {name!r}; known keys: {', '.join(SETTINGS)}")
    return SETTINGS[name]


def load_config(env: Mapping[str, str]) -> Config:
    home = Path(env.get("HOME", str(Path.home())))
    data_dir = Path(env.get("CLAUDE_COST_DATA_DIR") or home / ".local" / "share" / "claude-cost")
    config_path = config_path_for(env)
    raw = _read_raw(config_path)
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for name, s in SETTINGS.items():
        if name in raw:
            values[name], sources[name] = s.coerce(raw[name]), "config"
        else:
            values[name], sources[name] = s.default, "default"
    roots = [Path(p).expanduser() for p in values["data_roots"] or []] or [home / ".claude"]
    return Config(
        data_roots=roots,
        store_path=data_dir / "store.sqlite",
        config_path=config_path,
        timezone=values["timezone"],
        port=values["port"],
        top_n=values["top_n"],
        desktop_stores=[Path(p).expanduser() for p in values["desktop_session_stores"]],
        prices=values["prices"] or {},
        sources=sources,
    )


@dataclass(frozen=True)
class SettingView:
    name: str
    value: str  # value in effect, as shown
    default: str
    source: str  # "default" or "config"


def describe_settings(config_path: Path) -> list[SettingView]:
    raw = _read_raw(config_path)
    out = []
    for name, s in SETTINGS.items():
        if name in raw:
            out.append(SettingView(name, json.dumps(s.coerce(raw[name])), s.default_text, "config"))
        else:
            out.append(SettingView(name, s.default_text, s.default_text, "default"))
    return out


def get_value(config_path: Path, name: str) -> str:
    _setting(name)
    return next(v.value for v in describe_settings(config_path) if v.name == name)


def set_value(config_path: Path, name: str, text: str) -> str:
    """Validate and write one setting, keeping the file's comments. Returns the stored value."""
    s = _setting(name)
    value = s.coerce(s.parse(text))
    ensure_config_file(config_path)
    _read_raw(config_path)  # refuse to edit a file that is already broken
    lines = config_path.read_text().splitlines()
    new_line = f"{name} = {json.dumps(value)}"
    active = re.compile(rf"^{re.escape(name)}\s*=")
    commented = re.compile(rf"^#\s*{re.escape(name)}\s*=")
    start = next((i for i, l in enumerate(lines) if active.match(l)), None)
    if start is not None:
        end = start
        if lines[start].split("=", 1)[1].strip().startswith("[") and "]" not in lines[start]:
            end = next(i for i in range(start + 1, len(lines)) if "]" in lines[i])  # multi-line array
        lines[start:end + 1] = [new_line]
    else:
        at = next((i for i, l in enumerate(lines) if commented.match(l)), None)
        if at is not None:
            lines[at] = new_line
        else:
            lines.append(new_line)
    config_path.write_text("\n".join(lines) + "\n")
    _read_raw(config_path)
    return json.dumps(value)
