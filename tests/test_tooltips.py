"""Seam 2: dashboard metric tooltips and the merged Cold writes row, via the Flask test client."""
import re

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req


def _dashboard(root, configure):
    root.write_transcript("p", "s1", [
        req("a", "2026-10-29T10:00:00Z", write_1h=100_000),
        req("b", "2026-10-29T14:00:00Z", write_1h=100_000),   # 4h idle: Cold write
    ])
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    return create_app(factory, lambda: NOW).test_client().get("/").get_data(as_text=True)


def _row(html, label):
    return re.search(rf"<tr><td>{re.escape(label)}\b.*?</tr>", html, re.S).group(0)


def test_every_cost_component_and_cache_metric_has_a_tooltip(root, configure):
    html = _dashboard(root, configure)
    for label in ("Hit ratio", "Cold writes", "Warm writes", "5m writes", "1h writes"):
        assert 'class="info" title="' in _row(html, label), label
    for name in ("input", "output", "cache write (5m)", "cache write (1h)", "cache read"):
        assert re.search(rf"{re.escape(name)}\s*<span class=\"info\" title=\"", html), name


def test_5m_and_1h_tooltips_say_when_each_ttl_is_used(root, configure):
    html = _dashboard(root, configure)
    assert "always use the 5-minute cache by default" in _row(html, "5m writes")
    assert "once a subscription runs past its included usage" in _row(html, "5m writes")
    assert "on a Claude subscription within its included usage" in _row(html, "1h writes")
    assert "a short break doesn&#39;t trigger a Cold write" in _row(html, "1h writes")


def test_cold_writes_row_shows_request_count_and_rebuilds_is_gone(root, configure):
    html = _dashboard(root, configure)
    assert "Rebuilds" not in html
    assert "across 1 requests" in _row(html, "Cold writes")
