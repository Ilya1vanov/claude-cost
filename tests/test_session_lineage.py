"""Seams 1 and 2: copied-history Forks are Forks everywhere (query service and Session page)."""
import pytest

from claude_cost import ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import compact_line, req

M = 1_000_000


def fork_setup(root, parent="s1", child="s9"):
    root.write_transcript("p", parent, [req("m1", "2026-10-29T01:00:00Z", output=M),
                                        req("m2", "2026-10-29T03:00:00Z", output=M)])
    root.write_transcript("p", child, [req("m1", "2026-10-29T01:00:00Z", output=M, forked_from=parent),
                                       req("m9", "2026-10-29T05:00:00Z", output=M)])


def page(root, tmp_path, session_id):
    conn = store.connect(tmp_path / "page.sqlite")
    ingest.ingest(conn, [root.path])
    client = create_app(lambda: QueryService(conn, "UTC"), lambda: NOW).test_client()
    return client.get(f"/session/{session_id}").get_data(as_text=True)


def test_session_summaries_report_a_copied_history_fork(root, service_for):
    fork_setup(root)
    s = {x.session_id: x for x in service_for(root).session_summaries()}
    assert (s["s9"].parent_session_id, s["s9"].link_kind) == ("s1", "fork")
    assert (s["s9"].requests, s["s9"].cost) == (1, pytest.approx(20.0))
    assert (s["s1"].requests, s["s1"].cost) == (2, pytest.approx(40.0))


def test_fork_timeline_names_parent_and_parent_marks_the_fork(root, service_for):
    fork_setup(root)
    svc = service_for(root)
    child = svc.session_timeline("s9")
    assert (child.parent_session_id, child.link_kind) == ("s1", "fork")
    [mark] = svc.session_timeline("s1").side_sessions
    assert (mark.session_id, mark.kind, mark.before_index) == ("s9", "fork", 2)


def test_fork_session_page_reads_fork_session_of_parent(root, tmp_path):
    fork_setup(root)
    html = page(root, tmp_path, "s9")
    assert "Fork session of" in html and 'href="/session/s1"' in html
    parent = page(root, tmp_path, "s1")
    assert "Fork started" in parent and "Side session started" not in parent


def test_unknown_parent_session_is_plain_text_not_a_link(root, tmp_path):
    root.write_transcript("p", "s9", [req("mx", "2026-10-29T01:00:00Z", output=M, forked_from="gone"),
                                      req("m9", "2026-10-29T05:00:00Z", output=M)])
    html = page(root, tmp_path, "s9")
    assert "Fork session of" in html and "gone" in html
    assert 'href="/session/gone"' not in html


def test_dashboard_counts_equal_the_marks_on_the_page(root, service_for):
    fork_setup(root)
    svc = service_for(root)
    top = {s.key: s for s in svc.period_comparison("last7", NOW).top_sessions}
    marks = svc.session_timeline("s1").side_sessions
    assert top["s1"].forks == sum(m.kind == "fork" for m in marks) == 1


def test_pin_and_unpin_by_transcript_id_act_on_the_session(root, desktop, service_for):
    root.write_transcript("p", "a-old", [req("m1", "2026-10-29T01:00:00Z", output=M)])
    root.write_transcript("p", "b-new", [req("m2", "2026-10-29T02:00:00Z", output=M)])
    desktop.write_session("a", "b-new", priorCliSessionIds=["a-old"])
    svc = service_for(root, desktop=desktop)
    svc.pin_session("work", "b-new")
    assert svc.spotlight("work").pins == ("a-old",)
    svc.unpin_session("work", "b-new")
    assert svc.spotlight("work").pins == ()


def test_session_page_compactions_include_every_transcript(root, desktop, service_for):
    root.write_transcript("p", "a-old", [req("m1", "2026-10-29T01:00:00Z", output=M)],
                          extra_lines=[compact_line("c1", "2026-10-29T01:05:00Z", 1000, 80, "auto")])
    root.write_transcript("p", "b-new", [req("m2", "2026-10-29T02:00:00Z", output=M)],
                          extra_lines=[compact_line("c2", "2026-10-29T02:05:00Z", 1000, 80, "manual")])
    desktop.write_session("a", "b-new", priorCliSessionIds=["a-old"])
    t = service_for(root, desktop=desktop).session_timeline("a-old")
    assert [c.trigger for c in t.compactions] == ["auto", "manual"]


def _family(root, desktop):
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T01:00:00Z", output=M),
                                      req("m5", "2026-10-29T06:00:00Z", output=M)])
    root.write_subagent_transcript("p", "s1", "a1", [req("m2", "2026-10-29T02:00:00Z", output=M)],
                                   meta={"agentType": "fork", "isFork": True})
    root.write_transcript("p", "side1", [req("m3", "2026-10-29T03:00:00Z", output=M)])
    root.write_transcript("p", "fork1", [req("m4", "2026-10-29T04:00:00Z", output=M)])
    root.write_transcript("p", "cp1", [req("m1", "2026-10-29T01:00:00Z", output=M, forked_from="s1"),
                                       req("m6", "2026-10-29T05:00:00Z", output=M)])
    desktop.write_session("a", "s1")
    desktop.write_session("b", "side1", spawnedFrom={"sessionId": "local_a", "sideSession": True})
    desktop.write_session("c", "fork1", forkedFromSessionId="local_a")


def test_parent_page_labels_each_kind_and_never_marks_fork_subagents(root, desktop, service_for):
    _family(root, desktop)
    marks = service_for(root, desktop=desktop).session_timeline("s1").side_sessions
    assert sorted((m.session_id, m.kind) for m in marks) == [
        ("cp1", "fork"), ("fork1", "fork"), ("side1", "side")]
    by_id = {m.session_id: m for m in marks}
    assert [by_id[k].before_index for k in ("side1", "fork1", "cp1")] == [1, 1, 1]
    assert by_id["cp1"].ts > by_id["fork1"].ts


def test_top_sessions_counts_equal_marks_for_both_kinds(root, desktop, service_for):
    _family(root, desktop)
    svc = service_for(root, desktop=desktop)
    marks = svc.session_timeline("s1").side_sessions
    s1 = {s.key: s for s in svc.period_comparison("last7", NOW).top_sessions}["s1"]
    assert (s1.side_sessions, s1.forks) == (
        sum(m.kind == "side" for m in marks), sum(m.kind == "fork" for m in marks)) == (1, 2)
