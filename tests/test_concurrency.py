"""Seam 1 (QueryService): Concurrency, the Parallel sessions numbers on the period comparison's Usage."""
import pytest

from conftest import NOW
from helpers import req

D = "2026-10-29T"  # in this week; the previous week is 2026-10-19..25
B = "2026-10-22T"  # in the baseline week


def _usage(svc):
    return svc.period_comparison("week", NOW).usage_current


def test_two_sessions_in_one_slot_are_two_concurrent(root, service_for):
    root.write_transcript("p", "s1", [req("a", D + "10:00:30Z")])
    root.write_transcript("p", "s2", [req("b", D + "10:04:59Z")])
    u = _usage(service_for(root))
    assert (u.avg_concurrency, u.peak_concurrency) == (2, 2)


def test_a_subagent_only_slot_counts_its_session(root, service_for):
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z")])
    root.write_subagent_transcript("p", "s1", "ag", [req("b", D + "10:10:00Z")])
    root.write_transcript("p", "s2", [req("c", D + "10:11:00Z")])
    u = _usage(service_for(root))
    # slot 10:00 = {s1}; slot 10:10 = {s1 via its Subagent, s2}
    assert (u.avg_concurrency, u.peak_concurrency) == (1.5, 2)


def test_subagents_are_never_counted_as_sessions(root, service_for):
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z")])
    root.write_subagent_transcript("p", "s1", "ag1", [req("b", D + "10:01:00Z")])
    root.write_subagent_transcript("p", "s1", "ag2", [req("c", D + "10:02:00Z")])
    u = _usage(service_for(root))
    assert (u.avg_concurrency, u.peak_concurrency) == (1, 1)


def test_a_side_session_counts_separately_from_its_parent(root, desktop, service_for):
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z")])
    root.write_transcript("p", "side1", [req("b", D + "10:01:00Z")])
    desktop.write_session("a", "s1")
    desktop.write_session("b", "side1", spawnedFrom={"sessionId": "local_a", "sideSession": True})
    u = _usage(service_for(root, desktop=desktop))
    assert (u.avg_concurrency, u.peak_concurrency) == (2, 2)


def test_a_fork_counts_separately_and_its_copied_history_stays_with_the_parent(root, service_for):
    root.write_transcript("p", "s1", [req("m1", D + "10:00:00Z"), req("m2", D + "10:20:00Z")])
    root.write_transcript("p", "fork1", [req("m1", D + "10:00:00Z", forked_from="s1"),
                                         req("m9", D + "10:20:00Z")])
    u = _usage(service_for(root))
    # slot 10:00: only s1 (the fork's copy of m1 is owned by s1); slot 10:20: s1 and fork1
    assert (u.avg_concurrency, u.peak_concurrency) == (1.5, 2)


def test_idle_slots_do_not_pull_the_average_down(root, service_for):
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z"), req("b", D + "18:00:00Z")])
    root.write_transcript("p", "s2", [req("c", D + "10:00:00Z")])
    u = _usage(service_for(root))
    assert (u.avg_concurrency, u.peak_concurrency) == (1.5, 2)


def test_slots_are_clock_aligned_so_neighbouring_minutes_can_split(root, service_for):
    root.write_transcript("p", "s1", [req("a", D + "10:04:59Z")])
    root.write_transcript("p", "s2", [req("b", D + "10:05:00Z")])
    u = _usage(service_for(root))
    assert (u.avg_concurrency, u.peak_concurrency) == (1, 1)


def test_peak_is_the_busiest_slot(root, service_for):
    root.write_transcript("p", "s1", [req("a", D + "09:00:00Z"), req("d", D + "11:00:00Z")])
    root.write_transcript("p", "s2", [req("b", D + "11:01:00Z")])
    root.write_transcript("p", "s3", [req("c", D + "11:02:00Z")])
    u = _usage(service_for(root))
    assert u.peak_concurrency == 3
    assert u.avg_concurrency == 2


def test_no_requests_means_no_concurrency(root, service_for):
    root.write_transcript("p", "s1", [req("a", B + "10:00:00Z")])
    c = service_for(root).period_comparison("week", NOW)
    assert (c.usage_current.avg_concurrency, c.usage_current.peak_concurrency) == (None, None)


def test_baseline_has_its_own_concurrency(root, service_for):
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z"), req("b", B + "10:00:00Z")])
    root.write_transcript("p", "s2", [req("c", B + "10:01:00Z")])
    c = service_for(root).period_comparison("week", NOW)
    assert (c.usage_current.avg_concurrency, c.usage_current.peak_concurrency) == (1, 1)
    assert (c.usage_baseline.avg_concurrency, c.usage_baseline.peak_concurrency) == (2, 2)


# Seam 2: the dashboard's Parallel sessions card, via the Flask test client.
import re

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app


def _card(root, configure):
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z"), req("b", B + "10:00:00Z")])
    root.write_transcript("p", "s2", [req("c", D + "10:01:00Z"), req("d", D + "10:02:00Z"),
                                      req("e", B + "10:01:00Z")])
    root.write_transcript("p", "s3", [req("f", D + "10:03:00Z")])
    root.write_transcript("p", "s4", [req("g", B + "10:02:00Z")])
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    html = create_app(factory, lambda: NOW).test_client().get("/?period=week").get_data(as_text=True)
    return re.search(r'<section class="card" id="parallel-sessions">.*?</section>', html, re.S).group(0)


def test_parallel_sessions_card_shows_avg_and_peak_against_baseline(root, configure):
    card = _card(root, configure)
    assert "Parallel sessions" in card
    assert re.search(r"Avg concurrency.*?<div class=\"value\">3.0</div>\s*<div class=\"meta\">vs 3.0 in baseline", card, re.S)
    assert re.search(r"Peak concurrency.*?<div class=\"value\">3</div>\s*<div class=\"meta\">vs 3 in baseline", card, re.S)


def test_parallel_sessions_values_have_tooltips(root, configure):
    card = _card(root, configure)
    assert card.count('class="info" title="') == 2
    assert ("How many Sessions were working at once, on average. Time is cut into 5-minute slots; "
            "a Session counts in a slot if it or one of its Subagents made a request in it. Only slots "
            "where at least one Session was working are averaged, so idle time doesn&#39;t pull it down."
            ) in card
    assert "The most Sessions working at once in any single 5-minute slot" in card


def test_parallel_sessions_card_says_n_a_without_requests(root, configure):
    root.write_transcript("p", "s1", [req("a", "2026-09-01T10:00:00Z")])
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    html = create_app(factory, lambda: NOW).test_client().get("/?period=week").get_data(as_text=True)
    card = re.search(r'<section class="card" id="parallel-sessions">.*?</section>', html, re.S).group(0)
    assert card.count('<div class="value">n/a</div>') == 2
    assert card.count("vs n/a in baseline") == 2
