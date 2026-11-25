from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from claude_cost import ingest as ingest_mod
from claude_cost import pricing
from claude_cost import store
from claude_cost.query import QueryService

from helpers import DataRoot, DesktopStore

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """No test may read or write the real ~/.claude or user config."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_COST_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CLAUDE_COST_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture(autouse=True)
def no_real_browser_or_port(monkeypatch):
    """`serve` never probes the real port or drives the real Chrome in tests."""
    from claude_cost import cli
    monkeypatch.setattr(cli, "_probe_server", lambda url: None)
    monkeypatch.setattr(cli, "_focus_chrome_tab", lambda url: False)


@pytest.fixture(autouse=True)
def no_real_release_lookup(monkeypatch):
    """No test asks GitHub for the latest Release; tests inject a fake `releases`."""
    from claude_cost import releases
    monkeypatch.setattr(releases, "github_latest", lambda: None)


@pytest.fixture
def root(tmp_path) -> DataRoot:
    return DataRoot(tmp_path / "claude")


@pytest.fixture
def desktop(tmp_path) -> DesktopStore:
    return DesktopStore(tmp_path / "desktop" / "acct" / "org")


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    """Environment for the CLI/web adapters: temp config dir, temp store, UTC days."""
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    return {"HOME": str(tmp_path / "home"),
            "CLAUDE_COST_CONFIG_DIR": str(config),
            "CLAUDE_COST_DATA_DIR": str(tmp_path / "data")}


@pytest.fixture
def configure(env):
    """Write config.toml pointing at Data roots; returns env."""
    def _configure(*roots: DataRoot) -> dict[str, str]:
        import pathlib
        cfg = pathlib.Path(env["CLAUDE_COST_CONFIG_DIR"]) / "config.toml"
        items = ", ".join(f'"{r.path}"' for r in roots)
        cfg.write_text(f'data_roots = [{items}]\ntimezone = "UTC"\n')
        return env
    return _configure


@pytest.fixture
def service_for(tmp_path):
    """Ingest the given Data roots into a temp store; return a UTC QueryService."""
    def _service(*roots: DataRoot, prices: dict | None = None,
                 desktop: DesktopStore | None = None,
                 key_pattern: str | None = None, thresholds: dict | None = None,
                 compare_model: str | None = None) -> QueryService:
        conn = store.connect(tmp_path / "store.sqlite")
        table = pricing.build_table(prices)
        stores = [desktop.path.parent.parent] if desktop else []  # a parent dir is scanned
        ingest_mod.ingest(conn, [r.path for r in roots], price_table=table, desktop_stores=stores)
        extra = {"compare_model": compare_model} if compare_model else {}
        return QueryService(conn, "UTC", price_table=table, key_pattern=key_pattern,
                            thresholds=thresholds, **extra)
    return _service
