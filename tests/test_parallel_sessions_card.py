"""Seam 2: the Dashboard's Parallel sessions card, through the Flask test client."""
import re

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req

D = "2026-10-29T"  # in this week; the previous week is 2026-10-19..25
B = "2026-10-22T"  # in the baseline week


def _card(root, configure):
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    html = create_app(factory, lambda: NOW).test_client().get("/?period=week").get_data(as_text=True)
    return re.search(r'<section class="card" id="parallel-sessions">.*?</section>', html, re.S).group(0)


def _busy_week(root):
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z"), req("b", B + "10:00:00Z")])
    root.write_transcript("p", "s2", [req("c", D + "10:01:00Z"), req("d", D + "10:02:00Z"),
                                      req("e", B + "10:01:00Z")])
    root.write_transcript("p", "s3", [req("f", D + "10:03:00Z")])
    root.write_transcript("p", "s4", [req("g", B + "10:02:00Z")])


def test_parallel_sessions_card_shows_avg_and_peak_against_baseline(root, configure):
    _busy_week(root)
    card = _card(root, configure)
    assert "Parallel sessions" in card
    assert re.search(r"Avg concurrency.*?<div class=\"value\">3.0</div>\s*<div class=\"meta\">vs 3.0 in baseline", card, re.S)
    assert re.search(r"Peak concurrency.*?<div class=\"value\">3</div>\s*<div class=\"meta\">vs 3 in baseline", card, re.S)


def test_parallel_sessions_values_have_tooltips(root, configure):
    _busy_week(root)
    card = _card(root, configure)
    assert card.count('class="info" data-tip="') == 2
    assert ("How many Sessions were working at once, on average. Time is cut into 5-minute slots; "
            "a Session counts in a slot if it or one of its Subagents made a request in it. Only slots "
            "where at least one Session was working are averaged, so idle time doesn&#39;t pull it down."
            ) in card
    assert "The most Sessions working at once in any single 5-minute slot" in card


def test_parallel_sessions_card_says_n_a_without_requests(root, configure):
    root.write_transcript("p", "s1", [req("a", "2026-09-01T10:00:00Z")])
    card = _card(root, configure)
    assert card.count('<div class="value">n/a</div>') == 2
    assert card.count("vs n/a in baseline") == 2
