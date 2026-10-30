"""Seam 1: Subagents, Side sessions, Forks, Surface/Data root filters, through the query service."""
from datetime import date

import pytest

from helpers import DataRoot, req

D30 = date(2026, 10, 30)
M = 1_000_000  # output tokens = $20 on opus-5-5


def by_id(svc):
    return {s.session_id: s for s in svc.session_summaries()}


def test_subagent_spend_is_shown_separately_and_rolled_up(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=M)])
    root.write_subagent_transcript("p", "s1", "a1", [req("m2", "2026-10-30T02:00:00Z", output=M)],
                                   meta={"agentType": "Explore", "toolUseId": "toolu_1",
                                         "description": "look", "spawnDepth": 1})
    svc = service_for(root)
    s = by_id(svc)["s1"]
    assert (s.requests, s.cost) == (2, pytest.approx(40.0))
    assert (s.subagent_requests, s.subagent_cost) == (1, pytest.approx(20.0))
    [a] = svc.subagent_summaries()
    assert (a.session_id, a.agent_id, a.agent_type, a.tool_use_id, a.is_fork) == \
        ("s1", "a1", "Explore", "toolu_1", False)
    assert a.cost == pytest.approx(20.0)
    assert svc.daily_spend(D30, D30)[0].requests == 2


def test_fork_subagent_and_nested_subagent_metadata(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=M)])
    root.write_subagent_transcript("p", "s1", "f1", [req("m2", "2026-10-30T02:00:00Z", output=M)],
                                   meta={"agentType": "fork", "isFork": True})
    root.write_subagent_transcript("p", "s1", "n1", [req("m3", "2026-10-30T03:00:00Z", output=M)],
                                   meta={"agentType": "general-purpose", "parentAgentId": "f1",
                                         "spawnDepth": 2})
    svc = service_for(root)
    a = {x.agent_id: x for x in svc.subagent_summaries()}
    assert a["f1"].is_fork and a["f1"].agent_type == "fork"
    assert not a["n1"].is_fork and a["n1"].parent_agent_id == "f1"
    assert by_id(svc)["s1"].cost == pytest.approx(60.0)


def test_subagent_without_meta_and_superseded_dir(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=M)])
    root.write_subagent_transcript("p", "s1", "a1", [req("m2", "2026-10-30T02:00:00Z", output=M)])
    root.write_subagent_transcript("p", "s1.superseded-123", "a9",
                                   [req("m9", "2026-10-30T02:00:00Z", output=M)])
    svc = service_for(root)
    [a] = svc.subagent_summaries()
    assert a.agent_type is None
    assert svc.daily_spend(D30, D30)[0].requests == 2  # superseded copy ignored


def test_request_in_parent_and_subagent_counts_once_as_subagent(root, service_for):
    r = req("m2", "2026-10-30T02:00:00Z", output=M)
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=M), r])
    root.write_subagent_transcript("p", "s1", "a1", [r], meta={"agentType": "Explore"})
    svc = service_for(root)
    assert svc.daily_spend(D30, D30)[0].requests == 2
    assert by_id(svc)["s1"].subagent_requests == 1


def test_side_session_and_fork_link_to_parent_with_desktop_store(root, desktop, service_for):
    root.write_transcript("p", "cli-a", [req("m1", "2026-10-30T01:00:00Z", output=M)])
    root.write_transcript("p", "cli-b", [req("m2", "2026-10-30T02:00:00Z", output=M)])
    root.write_transcript("p", "cli-c", [req("m3", "2026-10-30T03:00:00Z", output=M)])
    desktop.write_session("a", "cli-a")
    desktop.write_session("b", "cli-b", spawnedFrom={"sessionId": "local_a", "sideSession": True})
    desktop.write_session("c", "cli-c", forkedFromSessionId="local_a")
    s = by_id(service_for(root, desktop=desktop))
    assert (s["cli-a"].parent_session_id, s["cli-a"].link_kind) == (None, None)
    assert (s["cli-b"].parent_session_id, s["cli-b"].link_kind) == ("cli-a", "side")
    assert (s["cli-c"].parent_session_id, s["cli-c"].link_kind) == ("cli-a", "fork")


def test_without_desktop_store_sessions_are_top_level(root, service_for):
    root.write_transcript("p", "cli-a", [req("m1", "2026-10-30T01:00:00Z", output=M)])
    root.write_transcript("p", "cli-b", [req("m2", "2026-10-30T02:00:00Z", output=M)])
    s = by_id(service_for(root))
    assert all(x.parent_session_id is None and x.link_kind is None for x in s.values())


def test_unresolvable_parent_stays_top_level(root, desktop, service_for):
    root.write_transcript("p", "cli-b", [req("m2", "2026-10-30T02:00:00Z", output=M)])
    desktop.write_session("b", "cli-b", spawnedFrom={"sessionId": "local_gone"})
    assert by_id(service_for(root, desktop=desktop))["cli-b"].parent_session_id is None


def test_prior_cli_session_ids_group_transcripts_into_one_session(root, desktop, service_for):
    root.write_transcript("p", "old", [req("m1", "2026-10-30T01:00:00Z", output=M)])
    root.write_transcript("p", "new", [req("m2", "2026-10-30T02:00:00Z", output=M)])
    desktop.write_session("a", "new", priorCliSessionIds=["old", "missing-on-disk"])
    s = by_id(service_for(root, desktop=desktop))
    assert list(s) == ["old"]
    assert (s["old"].requests, s["old"].cost) == (2, pytest.approx(40.0))
    assert set(s["old"].transcripts) == {"old", "new"}


def test_link_resolves_through_prior_ids_and_groups(root, desktop, service_for):
    """A child pointing at a Desktop session whose transcript was swapped links to the group."""
    root.write_transcript("p", "old", [req("m1", "2026-10-30T01:00:00Z", output=M)])
    root.write_transcript("p", "new", [req("m2", "2026-10-30T02:00:00Z", output=M)])
    root.write_transcript("p", "kid", [req("m3", "2026-10-30T03:00:00Z", output=M)])
    desktop.write_session("a", "new", priorCliSessionIds=["old"])
    desktop.write_session("k", "kid", spawnedFrom={"sessionId": "local_a"})
    assert (lambda s: (s["kid"].parent_session_id, s["kid"].link_kind))(
        by_id(service_for(root, desktop=desktop))) == ("old", "side")


def test_corrupt_desktop_file_is_skipped(root, desktop, service_for):
    root.write_transcript("p", "cli-a", [req("m1", "2026-10-30T01:00:00Z", output=M)])
    (desktop.path / "local_bad.json").write_text("{not json")
    (desktop.path / "deleted_x").write_text("1")
    assert list(by_id(service_for(root, desktop=desktop))) == ["cli-a"]


def test_surface_filter(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=M, entrypoint="cli"),
                                      req("m2", "2026-10-30T02:00:00Z", output=M, entrypoint="claude-desktop")])
    root.write_transcript("p", "s2", [req("m3", "2026-10-30T03:00:00Z", output=M, entrypoint="sdk-cli")])
    svc = service_for(root)
    day = lambda **kw: svc.daily_spend(D30, D30, **kw)[0].requests
    assert (day(), day(surface="CLI"), day(surface="Desktop"), day(surface="SDK")) == (3, 1, 1, 1)
    assert [x.session_id for x in svc.session_summaries(surface="SDK")] == ["s2"]
    assert svc.session_summaries(surface="Desktop")[0].requests == 1


def test_surface_filter_applies_to_subagents(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=M, entrypoint="cli")])
    root.write_subagent_transcript("p", "s1", "a1",
                                   [req("m2", "2026-10-30T02:00:00Z", output=M, entrypoint="cli")])
    svc = service_for(root)
    assert len(svc.subagent_summaries(surface="CLI")) == 1
    assert svc.subagent_summaries(surface="Desktop") == []


def test_data_root_filter(tmp_path, service_for):
    r1, r2 = DataRoot(tmp_path / "one"), DataRoot(tmp_path / "two")
    r1.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=M)])
    r2.write_transcript("p", "s2", [req("m2", "2026-10-30T02:00:00Z", output=M)])
    svc = service_for(r1, r2)
    assert svc.daily_spend(D30, D30)[0].requests == 2
    assert svc.daily_spend(D30, D30, data_root=r1.path)[0].requests == 1
    assert [x.session_id for x in svc.session_summaries(data_root=r2.path)] == ["s2"]


def test_links_survive_incremental_reingest_and_late_subagent(tmp_path, root, desktop):
    from claude_cost import ingest, store
    from claude_cost.query import QueryService
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=M)])
    conn = store.connect(tmp_path / "inc.sqlite")
    stores = [desktop.path.parent.parent]
    ingest.ingest(conn, [root.path], desktop_stores=stores)
    root.write_subagent_transcript("p", "s1", "a1", [req("m2", "2026-10-30T02:00:00Z", output=M)],
                                   meta={"agentType": "Explore"})
    ingest.ingest(conn, [root.path])  # desktop_stores=None keeps what was stored
    ingest.ingest(conn, [root.path], desktop_stores=stores)
    svc = QueryService(conn, "UTC")
    [s] = svc.session_summaries()
    assert (s.requests, s.subagent_requests) == (2, 1)
    assert [a.agent_type for a in svc.subagent_summaries()] == ["Explore"]
