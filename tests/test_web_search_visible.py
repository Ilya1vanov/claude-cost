"""Web search shown wherever Cost components are: Explore split, `prices`, charts, colours, tooltip."""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from claude_cost import cli, ingest, store
from claude_cost.explore import ExploreSpec
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req, web_search_result_line, web_search_use_line

TS = "2026-10-29T01:00:00Z"
TS2 = "2026-10-29T01:00:05Z"
CSS = Path(cli.__file__).parent / "static" / "app.css"


def _searching_root(root):
    root.write_transcript("p", "s1", [req("m1", TS, output=1_000_000)], title="PROJ-7 search",
                          extra_lines=[web_search_use_line(TS, "tu1", "m1"),
                                       web_search_result_line(TS2, "tu1", 2)])
    return root


def _client(env):
    cfg = cli.load_config(env)

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone, key_pattern=r"PROJ-\d+")
    return create_app(factory, lambda: NOW).test_client()


def test_cache_event_split_has_a_web_search_value_and_sums_to_total(root, service_for):
    svc = service_for(_searching_root(root))
    res = svc.explore(ExploreSpec("cache_event", {}))
    rows = {r.value: r for r in res.rows}
    assert rows["web search"].cost == pytest.approx(0.02)
    assert rows["uncached"].cost == pytest.approx(20.0)  # the fee is not folded into uncached
    assert sum(r.cost for r in res.rows) == pytest.approx(res.total_cost)


def test_prices_prints_the_per_search_rate_before_the_unpriced_line(root, configure):
    env = configure(_searching_root(root))
    out = io.StringIO()
    assert cli.main(["prices"], env=env, now=NOW, out=out) == 0
    lines = out.getvalue().strip().splitlines()
    assert lines[-1].startswith("Unpriced models found")
    assert "web search: $0.01 per search" in lines[-2]


def test_spotlight_chart_plots_web_search_as_its_own_series(root, configure):
    html = _client(configure(_searching_root(root))).get("/spotlights/PROJ-7").get_data(as_text=True)
    data = json.loads(re.search(r'id="chart-data"[^>]*>(.*?)</script>', html, re.S).group(1))
    series = {s["label"]: s["data"] for s in data["series"]}
    assert series["web search"] == [pytest.approx(0.02)]


def test_web_search_color_is_defined_and_distinct_in_both_themes():
    light, dark = CSS.read_text().split("prefers-color-scheme: dark", 1)
    for block in (light, dark):
        colors = dict(re.findall(r"--c-([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", block))
        assert "web-search" in colors
        others = [v.lower() for k, v in colors.items() if k != "web-search"]
        assert colors["web-search"].lower() not in others


def test_web_search_tooltip_says_per_search_not_tokens(root, configure):
    html = _client(configure(_searching_root(root))).get("/").get_data(as_text=True)
    assert re.search(r'web search\s*<span class="info" data-tip="[^"]*per search[^"]*not tokens', html)
