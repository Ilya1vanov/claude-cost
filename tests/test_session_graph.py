"""Seam: the session graph, loaded from a real ingested store."""
import pytest

from claude_cost import ingest, session_graph, store

from helpers import req

M = 1_000_000


@pytest.fixture
def graph_for(tmp_path):
    def _graph(root, desktop=None):
        conn = store.connect(tmp_path / "graph.sqlite")
        stores = [desktop.path.parent.parent] if desktop else []
        ingest.ingest(conn, [root.path], desktop_stores=stores)
        return session_graph.load(conn)
    return _graph


def t(root, sid, mid="m", ts="2026-10-30T01:00:00Z", **kw):
    root.write_transcript("p", sid, [req(f"{mid}-{sid}", ts, output=M, **kw)])


def test_desktop_side_and_fork_links(root, desktop, graph_for):
    for sid in ("cli-a", "cli-b", "cli-c"):
        t(root, sid)
    desktop.write_session("a", "cli-a")
    desktop.write_session("b", "cli-b", spawnedFrom={"sessionId": "local_a", "sideSession": True})
    desktop.write_session("c", "cli-c", forkedFromSessionId="local_a")
    g = graph_for(root, desktop)
    assert g.link("cli-a") is None
    assert g.link("cli-b") == ("cli-a", "side")
    assert g.link("cli-c") == ("cli-a", "fork")


def test_copied_history_makes_a_fork(root, graph_for):
    t(root, "s1")
    root.write_transcript("p", "s9", [req("m1-s1", "2026-10-30T01:00:00Z", output=M, forked_from="s1"),
                                      req("m9", "2026-10-30T05:00:00Z", output=M)])
    assert graph_for(root).link("s9") == ("s1", "fork")


def test_desktop_link_beats_copied_history(root, desktop, graph_for):
    t(root, "s1")
    t(root, "s2")
    root.write_transcript("p", "s9", [req("m1-s1", "2026-10-30T01:00:00Z", output=M, forked_from="s1"),
                                      req("m9", "2026-10-30T05:00:00Z", output=M)])
    desktop.write_session("2", "s2")
    desktop.write_session("9", "s9", spawnedFrom={"sessionId": "local_2"})
    assert graph_for(root, desktop).link("s9") == ("s2", "side")


def test_fork_of_a_fork_names_the_immediate_parent(root, graph_for):
    t(root, "s1")
    root.write_transcript("p", "s2", [req("m1-s1", "2026-10-30T01:00:00Z", output=M, forked_from="s1"),
                                      req("m2", "2026-10-30T02:00:00Z", output=M)])
    root.write_transcript("p", "s3", [req("m2", "2026-10-30T02:00:00Z", output=M, forked_from="s2"),
                                      req("m3", "2026-10-30T03:00:00Z", output=M)])
    g = graph_for(root)
    assert g.link("s3") == ("s2", "fork")
    assert g.link("s2") == ("s1", "fork")


def test_fork_of_a_fork_holding_its_grandparents_copies_names_the_immediate_parent(root, graph_for):
    t(root, "s1")
    root.write_transcript("p", "s3", [req("m1-s1", "2026-10-30T01:00:00Z", output=M, forked_from="s1"),
                                      req("m2", "2026-10-30T02:00:00Z", output=M, forked_from="s2"),
                                      req("m3", "2026-10-30T03:00:00Z", output=M)])
    assert graph_for(root).link("s3") == ("s2", "fork")


def test_fork_subagent_never_links(root, graph_for):
    t(root, "s1")
    root.write_subagent_transcript("p", "s1", "f1", [req("m2", "2026-10-30T02:00:00Z", output=M,
                                                          forked_from="s0")],
                                   meta={"agentType": "fork", "isFork": True})
    g = graph_for(root)
    assert g.link("s1") is None and g.children("s0") == []


def test_no_self_link_across_a_sessions_resumed_transcripts(root, desktop, graph_for):
    t(root, "a-old", ts="2026-10-29T01:00:00Z")
    root.write_transcript("p", "b-new", [req("m-a-old", "2026-10-29T01:00:00Z", output=M, forked_from="a-old"),
                                       req("m2", "2026-10-30T02:00:00Z", output=M)])
    desktop.write_session("a", "b-new", priorCliSessionIds=["a-old"])
    g = graph_for(root, desktop)
    assert g.session_of("b-new") == "a-old"
    assert g.link("a-old") is None
    assert g.children("a-old") == []


def test_link_kept_when_parent_session_is_unknown(root, graph_for):
    root.write_transcript("p", "s9", [req("mx", "2026-10-30T01:00:00Z", output=M, forked_from="gone"),
                                      req("m9", "2026-10-30T05:00:00Z", output=M)])
    g = graph_for(root)
    assert g.link("s9") == ("gone", "fork")
    assert g.children("gone") == ["s9"]


def test_transcripts_and_session_of(root, desktop, graph_for):
    t(root, "a-old", ts="2026-10-29T01:00:00Z")
    t(root, "b-new", ts="2026-10-30T02:00:00Z")
    t(root, "solo")
    desktop.write_session("a", "b-new", priorCliSessionIds=["a-old"])
    g = graph_for(root, desktop)
    assert (g.session_of("a-old"), g.session_of("b-new"), g.session_of("solo")) == ("a-old", "a-old", "solo")
    assert sorted(g.transcripts("a-old")) == ["a-old", "b-new"]
    assert g.transcripts("solo") == ["solo"]
    assert g.transcripts("unknown") == ["unknown"] and g.session_of("unknown") == "unknown"


def test_children_sorted_by_id(root, graph_for):
    t(root, "s1")
    for sid in ("c", "a", "b"):
        root.write_transcript("p", sid, [req("m1-s1", "2026-10-30T01:00:00Z", output=M, forked_from="s1"),
                                         req(f"x{sid}", "2026-10-30T05:00:00Z", output=M)])
    assert graph_for(root).children("s1") == ["a", "b", "c"]
