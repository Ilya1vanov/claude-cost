"""Seam 1 + 2: Top sessions rows carry label, Subagents and Side session/Fork counts."""
import re

import pytest

from claude_cost import ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req

M = 1_000_000  # $20 of output tokens


def _family(root, desktop):
    """s1: own $20 + one Subagent $20; a Side session and a Fork of it ($20 each)."""
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T01:00:00Z", output=M)])
    root.write_subagent_transcript("p", "s1", "a1", [req("m2", "2026-10-29T02:00:00Z", output=M)],
                                   meta={"agentType": "Explore"})
    root.write_transcript("p", "side1", [req("m3", "2026-10-29T03:00:00Z", output=M)])
    root.write_transcript("p", "fork1", [req("m4", "2026-10-29T04:00:00Z", output=M)])
    desktop.write_session("a", "s1")
    desktop.write_session("b", "side1", spawnedFrom={"sessionId": "local_a", "sideSession": True})
    desktop.write_session("c", "fork1", forkedFromSessionId="local_a")


def test_ranked_session_carries_subagents_and_side_session_counts(root, desktop, service_for):
    _family(root, desktop)
    c = service_for(root, desktop=desktop).period_comparison("last7", NOW)
    top = {s.key: s for s in c.top_sessions}
    s1 = top["s1"]
    assert (s1.cost, s1.requests) == (pytest.approx(40.0), 2)   # total unchanged
    assert (s1.subagents, s1.subagent_cost) == (1, pytest.approx(20.0))
    assert (s1.side_sessions, s1.forks) == (1, 1)
    assert s1.label == "s1"
    assert (top["side1"].cost, top["side1"].side_sessions) == (pytest.approx(20.0), 0)


def test_dashboard_row_shows_label_subagents_and_not_included_note(root, desktop, tmp_path):
    _family(root, desktop)
    conn = store.connect(tmp_path / "store.sqlite")
    ingest.ingest(conn, [root.path], desktop_stores=[desktop.path.parent.parent])
    html = create_app(lambda: QueryService(conn, "UTC"), lambda: NOW).test_client() \
        .get("/").get_data(as_text=True)
    row = re.search(r'<tr><td class="rank">1</td>.*?</tr>', html, re.S).group(0)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row))
    assert 'title="s1"' in row
    assert '<td class="num">$40.00</td><td class="num">2</td>' in row
    assert "1 subagent = $20.00" in text
    assert "1 side session, 1 fork not included" in text


def test_side_session_and_fork_counts_only_cover_those_started_in_the_period(root, desktop, service_for):
    _family(root, desktop)
    root.write_transcript("p", "oldside", [req("m5", "2026-09-01T03:00:00Z", output=M)])
    root.write_transcript("p", "oldfork", [req("m6", "2026-09-01T04:00:00Z", output=M)])
    desktop.write_session("d", "oldside", spawnedFrom={"sessionId": "local_a", "sideSession": True})
    desktop.write_session("e", "oldfork", forkedFromSessionId="local_a")
    top = {s.key: s for s in service_for(root, desktop=desktop).period_comparison("last7", NOW).top_sessions}
    assert (top["s1"].side_sessions, top["s1"].forks) == (1, 1)


def test_cli_fork_by_copied_history_counts_as_a_fork_of_its_parent(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T01:00:00Z", output=M)])
    root.write_transcript("p", "s9", [req("m1", "2026-10-29T01:00:00Z", output=M, forked_from="s1"),
                                      req("m9", "2026-10-29T05:00:00Z", output=M)])
    top = {s.key: s for s in service_for(root).period_comparison("last7", NOW).top_sessions}
    assert (top["s1"].side_sessions, top["s1"].forks) == (0, 1)


def test_linked_session_whose_requests_are_all_subagent_owned_does_not_break_the_dashboard(
        root, desktop, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T01:00:00Z", output=M)])
    root.write_subagent_transcript("p", "side1", "a1", [req("m2", "2026-10-29T02:00:00Z", output=M)])
    desktop.write_session("a", "s1")
    desktop.write_session("b", "side1", spawnedFrom={"sessionId": "local_a", "sideSession": True})
    top = {s.key: s for s in service_for(root, desktop=desktop).period_comparison("last7", NOW).top_sessions}
    assert top["s1"].cost == pytest.approx(20.0)


def test_dashboard_rolls_up_sessions_once_per_request(root, desktop, tmp_path, monkeypatch):
    _family(root, desktop)
    conn = store.connect(tmp_path / "store.sqlite")
    ingest.ingest(conn, [root.path], desktop_stores=[desktop.path.parent.parent])
    calls = []
    conn.set_trace_callback(lambda sql: calls.append(1) if "FROM session_relations" in sql else None)
    create_app(lambda: QueryService(conn, "UTC"), lambda: NOW).test_client().get("/")
    assert len(calls) == 1
