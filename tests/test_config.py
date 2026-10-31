from __future__ import annotations

import io
import tomllib
from pathlib import Path

import pytest

from claude_cost import cli
from claude_cost.config import ConfigError, load_config

from conftest import NOW


def run(argv, env, **kw):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, env=env, now=NOW, out=out, err=err, **kw)
    return code, out.getvalue(), err.getvalue()


def cfg_file(env) -> Path:
    return Path(env["CLAUDE_COST_CONFIG_DIR"]) / "config.toml"


def test_first_run_creates_commented_defaults(env, root):
    assert not cfg_file(env).exists()
    code, _, _ = run(["config", "path"], env)
    assert code == 0
    text = cfg_file(env).read_text()
    assert tomllib.loads(text) == {}  # every default is commented out
    for key in ("data_roots", "port", "top_n", "timezone", "desktop_session_stores"):
        assert f"# {key} = " in text


def test_any_command_creates_the_file_and_defaults_apply(env, tmp_path):
    code, out, _ = run(["today"], env)
    assert code == 0 and cfg_file(env).exists()
    cfg = load_config(env)
    assert (cfg.port, cfg.top_n, cfg.timezone) == (8765, 10, None)
    assert cfg.data_roots == [Path(env["HOME"]) / ".claude"]
    assert set(cfg.sources.values()) == {"default"}


def test_config_prints_value_default_and_source(env):
    code, out, _ = run(["config", "set", "port", "9000"], env)
    code, out, _ = run(["config"], env)
    assert code == 0
    lines = {l.split(" = ")[0]: l for l in out.splitlines()}
    assert {"data_roots", "port", "top_n", "timezone", "prices", "desktop_session_stores", "spotlight_key_pattern"} <= set(lines)  # plus finding thresholds
    assert "port = 9000  (default: 8765, source: config)" in lines["port"]
    assert "top_n = 10  (default: 10, source: default)" in lines["top_n"]
    assert "source: default" in lines["data_roots"] and "~/.claude" in lines["data_roots"]


def test_get_and_set_roundtrip_and_values_are_read_from_config(env, tmp_path):
    assert run(["config", "set", "top_n", "25"], env)[0] == 0
    assert run(["config", "set", "port", "9100"], env)[0] == 0
    assert run(["config", "set", "timezone", "Europe/Berlin"], env)[0] == 0
    a, b = tmp_path / "a", tmp_path / "b with space"
    assert run(["config", "set", "data_roots", f"{a}, {b}"], env)[0] == 0
    assert run(["config", "get", "top_n"], env)[1].strip() == "25"
    cfg = load_config(env)
    assert (cfg.top_n, cfg.port, cfg.timezone) == (25, 9100, "Europe/Berlin")
    assert cfg.data_roots == [a, b]
    assert cfg.sources["port"] == "config"
    # file stays valid TOML and keeps the comments
    assert tomllib.loads(cfg_file(env).read_text())["port"] == 9100
    assert "# Number of items in the top-N cards." in cfg_file(env).read_text()


def test_set_overwrites_and_does_not_duplicate(env):
    run(["config", "set", "port", "9000"], env)
    run(["config", "set", "port", "9001"], env)
    assert cfg_file(env).read_text().count("port = ") == 1  # the commented default is replaced in place
    assert load_config(env).port == 9001


def test_set_replaces_multiline_array_written_by_hand(env):
    run(["config", "path"], env)
    cfg_file(env).write_text('data_roots = [\n  "/x",\n  "/y",\n]\nport = 1234\n')
    assert run(["config", "set", "data_roots", "/z"], env)[0] == 0
    cfg = load_config(env)
    assert cfg.data_roots == [Path("/z")] and cfg.port == 1234


@pytest.mark.parametrize("key,value,message", [
    ("nope", "1", "unknown config key"),
    ("port", "abc", "must be an integer"),
    ("port", "0", "between 1 and 65535"),
    ("port", "70000", "between 1 and 65535"),
    ("top_n", "0", ">= 1"),
    ("top_n", "1.5", "must be an integer"),
    ("timezone", "Mars/Olympus", "not a known IANA"),
    ("data_roots", " , ", "non-empty list"),
])
def test_set_rejects_bad_input_and_leaves_file_unchanged(env, key, value, message):
    run(["config", "path"], env)
    before = cfg_file(env).read_text()
    code, _, err = run(["config", "set", key, value], env)
    assert code == 2 and message in err
    assert cfg_file(env).read_text() == before


def test_get_unknown_key_is_an_error(env):
    code, _, err = run(["config", "get", "nope"], env)
    assert code == 2 and "unknown config key" in err


def test_invalid_file_value_is_reported_for_other_commands(env):
    run(["config", "path"], env)
    cfg_file(env).write_text('port = "high"\n')
    code, _, err = run(["today"], env)
    assert code == 2 and "port must be an integer" in err
    with pytest.raises(ConfigError):
        load_config(env)


def test_invalid_toml_is_reported_but_path_and_edit_still_work(env):
    run(["config", "path"], env)
    cfg_file(env).write_text("port = = 3\n")
    assert "not valid TOML" in run(["today"], env)[2]
    assert run(["config", "path"], env)[1].strip() == str(cfg_file(env))
    calls = []
    assert run(["config", "edit"], {**env, "EDITOR": "myed --wait"}, run_editor=calls.append)[0] == 0
    assert calls == [["myed", "--wait", str(cfg_file(env))]]


def test_path_prints_location(env):
    assert run(["config", "path"], env)[1].strip() == str(cfg_file(env))


def test_edit_uses_editor_env_and_defaults_to_vi(env):
    calls = []
    run(["config", "edit"], env, run_editor=calls.append)
    run(["config", "edit"], {**env, "EDITOR": "nano"}, run_editor=calls.append)
    assert calls == [["vi", str(cfg_file(env))], ["nano", str(cfg_file(env))]]


def test_serve_uses_configured_port(env, root):
    run(["config", "set", "port", "9321"], env)
    run(["config", "set", "data_roots", str(root.path)], env)
    seen = []
    code, out, _ = run(["serve"], env, run_server=lambda app, host, port: seen.append((host, port)),
                       open_browser=lambda url: None)
    assert code == 0 and seen == [("127.0.0.1", 9321)]


# --- Desktop session store detected by default (macOS) ---

@pytest.fixture
def mac(monkeypatch):
    from claude_cost import config
    monkeypatch.setattr(config, "_is_macos", lambda: True)


def _desktop_folder(env) -> Path:
    return Path(env["HOME"]) / "Library" / "Application Support" / "Claude" / "claude-code-sessions"


def test_desktop_default_resolves_to_the_detected_folder_and_links_side_sessions(mac, root, env):
    from helpers import DesktopStore, req
    from claude_cost import ingest, store
    from claude_cost.query import QueryService
    folder = _desktop_folder(env)
    desktop = DesktopStore(folder / "acct" / "org")
    root.write_transcript("p", "old", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000)])
    root.write_transcript("p", "new", [req("m2", "2026-10-30T02:00:00Z", output=1_000_000)])
    desktop.write_session("a", "new", priorCliSessionIds=["old"])
    cfg_file(env).parent.mkdir(parents=True, exist_ok=True)
    cfg_file(env).write_text(f'timezone = "UTC"\ndata_roots = ["{root.path}"]\n')
    cfg = load_config(env)
    assert cfg.desktop_stores == [folder]
    assert cfg.sources["desktop_session_stores"] == "default (detected)"
    assert run(["today"], env)[0] == 0
    summaries = QueryService(store.connect(cfg.store_path), "UTC").session_summaries()
    assert [(s.session_id, s.requests) for s in summaries] == [("old", 2)]


def test_desktop_default_is_empty_when_the_folder_is_missing(mac, env):
    cfg = load_config(env)
    assert cfg.desktop_stores == []
    assert cfg.sources["desktop_session_stores"] == "default"


def test_desktop_default_is_not_detected_off_macos(env, monkeypatch):
    from claude_cost import config
    monkeypatch.setattr(config, "_is_macos", lambda: False)
    _desktop_folder(env).mkdir(parents=True)
    assert load_config(env).desktop_stores == []


def test_explicit_empty_list_turns_desktop_links_off(mac, env):
    _desktop_folder(env).mkdir(parents=True)
    run(["config", "path"], env)
    cfg_file(env).write_text("desktop_session_stores = []\n")
    cfg = load_config(env)
    assert cfg.desktop_stores == []
    assert cfg.sources["desktop_session_stores"] == "config"


def test_config_get_and_list_show_the_detected_value_and_source(mac, env):
    folder = _desktop_folder(env)
    folder.mkdir(parents=True)
    code, out, _ = run(["config", "get", "desktop_session_stores"], env)
    assert code == 0 and str(folder) in out and "default (detected)" in out
    assert "detected" not in run(["config", "get", "port"], env)[1]
    line = next(l for l in run(["config"], env)[1].splitlines() if l.startswith("desktop_session_stores"))
    assert str(folder) in line and "source: default (detected)" in line


def test_fresh_config_comments_the_detected_path(mac, env):
    folder = _desktop_folder(env)
    folder.mkdir(parents=True)
    run(["config", "path"], env)
    text = cfg_file(env).read_text()
    assert f'# desktop_session_stores = ["{folder}"]' in text
    assert tomllib.loads(text) == {}
def test_serve_opens_browser_by_default(env, root):
    run(["config", "set", "data_roots", str(root.path)], env)
    opened = []
    run(["serve"], env, run_server=lambda app, host, port: None, open_browser=opened.append)
    assert opened == ["http://127.0.0.1:8765/"]


@pytest.mark.parametrize("argv, extra_env", [
    (["serve", "--browser=none"], {}),
    (["serve"], {"BROWSER": "none"}),
])
def test_serve_skips_browser_when_none(env, root, argv, extra_env):
    run(["config", "set", "data_roots", str(root.path)], env)
    opened = []
    code, out, _ = run(argv, {**env, **extra_env}, run_server=lambda app, host, port: None,
                       open_browser=opened.append)
    assert code == 0 and opened == [] and "Serving http://127.0.0.1:8765/" in out
