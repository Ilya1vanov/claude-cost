"""Seam 1: Session timeline through the query service."""
import pytest

from helpers import compact_line, req

M = 1_000_000


def _session(root):
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-30T01:00:00Z", input=10, cache_read=0, write_5m=1000, output=100),
        req("m2", "2026-10-30T01:01:00Z", input=10, cache_read=1000, write_5m=500, output=100),
        # 2h idle: cache expired, Cold write
        req("m3", "2026-10-30T03:01:00Z", input=10, cache_read=0, write_1h=1510, output=100),
        req("m4", "2026-10-30T03:02:00Z", input=10, cache_read=1510, output=100),
    ], extra_lines=[compact_line("c1", "2026-10-30T03:02:30Z", 150_000, 8_000, "auto"),
                    compact_line("c1", "2026-10-30T03:02:30Z", 150_000, 8_000, "auto")])
    root.write_subagent_transcript("p", "s1", "a1", [req("sa1", "2026-10-30T01:00:30Z", output=M)],
                                   meta={"agentType": "Explore", "description": "look"})


def test_timeline_has_context_cost_idle_cold_compaction_and_subagent(root, service_for):
    _session(root)
    t = service_for(root).session_timeline("s1")
    assert [p.message_id for p in t.points] == ["m1", "m2", "m3", "m4"]  # Subagent request excluded
    assert [p.context_tokens for p in t.points] == [1010, 1510, 1520, 1520]
    assert t.points[1].cost > 0 and t.cost == pytest.approx(sum(p.cost for p in t.points))
    assert [p.idle for p in t.points] == [False, False, True, False]
    assert t.points[2].gap_seconds == 7200
    assert [p.cache_event for p in t.points] == ["warm", "warm", "cold", "none"]
    [c] = t.compactions  # duplicate uuid counted once
    assert (c.pre_tokens, c.post_tokens, c.trigger, c.before_index) == (150_000, 8_000, "auto", 4)
    [a] = t.subagents
    assert (a.agent_id, a.agent_type, a.requests, a.before_index) == ("a1", "Explore", 1, 1)
    assert a.cost == pytest.approx(20.0)


def test_subagent_has_its_own_timeline(root, service_for):
    _session(root)
    svc = service_for(root)
    t = svc.session_timeline("s1", agent_id="a1")
    assert [p.message_id for p in t.points] == ["sa1"] and t.agent_id == "a1"
    assert svc.session_timeline("s1", agent_id="nope") is None
    assert svc.session_timeline("nope") is None


def test_side_session_is_marked_and_has_its_own_timeline(root, desktop, service_for):
    root.write_transcript("p", "cli-a", [req("m1", "2026-10-30T01:00:00Z", output=M),
                                         req("m3", "2026-10-30T01:10:00Z", output=M)])
    root.write_transcript("p", "cli-b", [req("m2", "2026-10-30T01:05:00Z", output=M)])
    desktop.write_session("a", "cli-a")
    desktop.write_session("b", "cli-b", spawnedFrom={"sessionId": "local_a", "sideSession": True})
    svc = service_for(root, desktop=desktop)
    [s] = svc.session_timeline("cli-a").side_sessions
    assert (s.session_id, s.before_index, s.requests) == ("cli-b", 1, 1)
    child = svc.session_timeline("cli-b")
    assert (child.parent_session_id, child.link_kind) == ("cli-a", "side")


def test_idle_gap_and_cold_resume_marks_follow_configured_thresholds(root, service_for):
    _session(root)
    t = service_for(root).session_timeline("s1")
    assert [p.idle for p in t.points] == [False, False, True, False]
    assert not any(p.cold_resume for p in t.points)   # 1.5K context is below the 100K default
    t = service_for(root, thresholds={"timeline_idle_gap_seconds": 8_000,
                                      "cold_resume_min_context": 1_000}).session_timeline("s1")
    assert not any(p.idle for p in t.points)
    assert [p.cold_resume for p in t.points] == [False, False, True, False]
    t = service_for(root, thresholds={"cold_resume_min_context": 1_000,
                                      "cold_resume_idle_minutes": 180}).session_timeline("s1")
    assert not any(p.cold_resume for p in t.points)   # the 2h gap is under 180 minutes
