"""Seam 1 (QueryService): Concurrency, the Parallel sessions numbers on the period comparison's Usage."""
from datetime import datetime, timezone

import pytest

from claude_cost import store
from claude_cost.query import QueryService
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


def test_subagents_per_session_is_the_strips_subagents_over_sessions(root, service_for):
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z")])
    root.write_subagent_transcript("p", "s1", "x1", [req("b", D + "10:01:00Z")])
    root.write_subagent_transcript("p", "s1", "x2", [req("c", D + "10:02:00Z")],
                                   meta={"parentAgentId": "x1", "spawnDepth": 2})  # nested counts
    root.write_subagent_transcript("p", "s1", "f1", [req("d", D + "10:03:00Z")],
                                   meta={"agentType": "fork", "isFork": True})  # Fork subagent counts
    root.write_transcript("p", "s2", [req("e", D + "10:04:00Z")])  # no Subagents, still in the denominator
    u = _usage(service_for(root))
    assert (u.sessions, u.subagents) == (2, 3)
    assert u.subagents_per_session == 1.5


def test_subagents_per_session_is_none_without_sessions_and_baseline_has_its_own(root, service_for):
    root.write_transcript("p", "s1", [req("a", B + "10:00:00Z")])
    root.write_subagent_transcript("p", "s1", "x1", [req("b", B + "10:01:00Z")])
    root.write_transcript("p", "s2", [req("c", B + "10:02:00Z")])
    c = service_for(root).period_comparison("week", NOW)
    assert c.usage_current.subagents_per_session is None
    assert c.usage_baseline.subagents_per_session == 0.5


def test_sessions_with_subagents_share_counts_only_sessions_that_ran_one(root, service_for):
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z")])
    root.write_subagent_transcript("p", "s1", "x1", [req("b", D + "10:01:00Z")])
    root.write_subagent_transcript("p", "s1", "x2", [req("c", D + "10:02:00Z")])  # still one Session
    root.write_transcript("p", "s2", [req("d", D + "10:03:00Z")])
    root.write_transcript("p", "s3", [req("e", D + "10:04:00Z")])
    root.write_transcript("p", "s4", [req("f", D + "10:05:00Z")])
    u = _usage(service_for(root))
    assert (u.sessions, u.sessions_with_subagents) == (4, 1)
    assert u.share_sessions_with_subagents == 0.25


def test_a_subagent_of_a_side_session_counts_for_the_side_session_not_its_parent(root, desktop, service_for):
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z")])
    root.write_transcript("p", "side1", [req("b", D + "10:01:00Z")])
    root.write_subagent_transcript("p", "side1", "x1", [req("c", D + "10:02:00Z")])
    desktop.write_session("a", "s1")
    desktop.write_session("b", "side1", spawnedFrom={"sessionId": "local_a", "sideSession": True})
    u = _usage(service_for(root, desktop=desktop))
    assert (u.sessions, u.sessions_with_subagents) == (2, 1)
    assert u.share_sessions_with_subagents == 0.5


def test_a_subagent_of_a_fork_counts_for_the_fork_not_its_parent(root, service_for):
    root.write_transcript("p", "s1", [req("m1", D + "10:00:00Z")])
    root.write_transcript("p", "fork1", [req("m1", D + "10:00:00Z", forked_from="s1"),
                                         req("m9", D + "10:20:00Z")])
    root.write_subagent_transcript("p", "fork1", "x1", [req("c", D + "10:21:00Z")])
    u = _usage(service_for(root))
    assert (u.sessions, u.sessions_with_subagents) == (2, 1)


def test_a_session_active_only_through_its_subagent_is_a_session_using_subagents(root, service_for):
    root.write_transcript("p", "s1", [req("a", "2026-10-20T10:00:00Z")])  # before the Period
    root.write_subagent_transcript("p", "s1", "x1", [req("b", D + "10:00:00Z")])
    u = _usage(service_for(root))
    assert (u.sessions, u.sessions_with_subagents) == (1, 1)
    assert u.share_sessions_with_subagents == 1


def test_share_of_sessions_with_subagents_is_none_without_sessions_and_baseline_has_its_own(root, service_for):
    root.write_transcript("p", "s1", [req("a", B + "10:00:00Z")])
    root.write_subagent_transcript("p", "s1", "x1", [req("b", B + "10:01:00Z")])
    root.write_transcript("p", "s2", [req("c", B + "10:02:00Z")])
    c = service_for(root).period_comparison("week", NOW)
    assert c.usage_current.share_sessions_with_subagents is None
    assert c.usage_baseline.share_sessions_with_subagents == 0.5


def test_the_repeated_local_hour_at_dst_fall_back_is_two_slots(root, tmp_path, service_for):
    # New York leaves DST on 2026-11-01: 05:30Z and 06:30Z are both 01:30 local, an hour apart.
    root.write_transcript("p", "s1", [req("a", "2026-11-01T05:30:00Z")])
    root.write_transcript("p", "s2", [req("b", "2026-11-01T06:30:00Z")])
    service_for(root)
    ny = QueryService(store.connect(tmp_path / "store.sqlite"), "America/New_York")
    u = ny.period_comparison("week", datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)).usage_current
    assert (u.avg_concurrency, u.peak_concurrency) == (1, 1)
