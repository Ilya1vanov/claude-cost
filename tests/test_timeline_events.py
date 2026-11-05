"""Seam 1: the Timeline's event rows (setting change, compaction, idle gap, side session or fork
started, forked from) through the query service, and the JSON and page that carry them."""
from claude_cost import ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import compact_line, compact_summary_line, prompt_line, req

M = 1_000_000


def kinds(rows):
    return [r.kind for r in rows]


def of_kind(rows, kind):
    return [r for r in rows if r.kind == kind]


def test_a_setting_change_row_sits_before_the_request_that_made_it(root, service_for):
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-30T01:00:00Z", model="claude-opus-5-5", effort="high"),
        req("m2", "2026-10-30T01:00:10Z", model="claude-opus-5-5", effort="medium")])
    rows = service_for(root).session_timeline("s1", with_rows=True).rows
    assert kinds(rows) == ["request", "setting_change", "request"]
    change = rows[1]
    assert (change.from_model, change.from_effort, change.to_model, change.to_effort) == (
        "claude-opus-5-5", "high", "claude-opus-5-5", "medium")
    assert change.ts == rows[2].ts


def test_a_compaction_row_carries_tokens_late_flag_and_its_summary(root, service_for):
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-30T01:00:00Z", input=900_000),
        req("m2", "2026-10-30T01:00:10Z", input=5_000)], extra_lines=[
        compact_line("c1", "2026-10-30T01:00:05Z", 900_000, 5_000, "auto"),
        compact_summary_line("cs1", "2026-10-30T01:00:06Z", parent_uuid="c1", text="We fixed the bug.")])
    rows = service_for(root).session_timeline("s1", with_rows=True).rows
    assert kinds(rows) == ["request", "compaction", "request"]
    c = rows[1]
    assert (c.trigger, c.pre_tokens, c.post_tokens, c.late) == ("auto", 900_000, 5_000, True)
    assert c.summary == "We fixed the bug."


def test_a_compaction_summary_is_empty_when_the_transcript_is_gone(root, service_for):
    f = root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z"),
                                          req("m2", "2026-10-30T01:00:10Z")], extra_lines=[
        compact_line("c1", "2026-10-30T01:00:05Z", 10, 5),
        compact_summary_line("cs1", "2026-10-30T01:00:06Z", parent_uuid="c1")])
    service = service_for(root)
    f.unlink()
    [c] = of_kind(service.session_timeline("s1", with_rows=True).rows, "compaction")
    assert (c.pre_tokens, c.summary) == (10, "")


def test_an_idle_gap_row_follows_the_request_before_the_gap(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z"),
                                      req("m2", "2026-10-30T03:00:00Z")])
    rows = service_for(root).session_timeline("s1", with_rows=True).rows
    assert kinds(rows) == ["request", "idle_gap", "request"]
    assert (rows[1].ts, rows[1].gap_seconds) == (rows[0].ts, 7200)


def test_the_parent_marks_a_fork_started_and_the_fork_opens_with_forked_from(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T01:00:00Z", output=M),
                                      req("m2", "2026-10-29T03:00:00Z", output=M)])
    root.write_transcript("p", "s9", [req("m1", "2026-10-29T01:00:00Z", output=M, forked_from="s1"),
                                      req("m9", "2026-10-29T05:00:00Z", output=M)])
    svc = service_for(root)
    fork = svc.session_timeline("s9", with_rows=True).rows
    assert kinds(fork) == ["forked_from", "request"]  # only the request the fork owns
    parent = svc.session_timeline("s1", with_rows=True).rows
    fork_point = parent[0].ts  # the last request the fork copied: m1
    assert (fork[0].parent_session_id, fork[0].ts) == ("s1", fork_point) and fork_point < fork[1].ts
    [started] = of_kind(parent, "child_session")
    assert (started.session_id, started.link_kind, started.requests) == ("s9", "fork", 1)
    assert started.ts == fork_point


def test_a_side_session_is_marked_started_in_its_parent_and_no_forked_from_in_it(root, desktop,
                                                                                  service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T01:00:00Z"),
                                      req("m2", "2026-10-29T03:00:00Z")])
    root.write_transcript("p", "s2", [req("m3", "2026-10-29T02:00:00Z")])
    desktop.write_session("a", "s1")
    desktop.write_session("b", "s2", spawnedFrom={"sessionId": "local_a", "sideSession": True})
    svc = service_for(root, desktop=desktop)
    [started] = of_kind(svc.session_timeline("s1", with_rows=True).rows, "child_session")
    assert (started.session_id, started.link_kind) == ("s2", "side")
    assert "forked_from" not in kinds(svc.session_timeline("s2", with_rows=True).rows)


def test_events_and_prompts_interleave_in_time_order(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:10Z", effort="high"),
                                      req("m2", "2026-10-30T02:00:10Z", effort="low")],
                          extra_lines=[prompt_line("u1", "2026-10-30T01:00:00Z"),
                                       prompt_line("u2", "2026-10-30T02:00:00Z")])
    rows = service_for(root).session_timeline("s1", with_rows=True).rows
    assert kinds(rows) == ["prompt", "request", "idle_gap", "prompt", "setting_change", "request"]


def _client(root, tmp_path):
    path = tmp_path / "events.sqlite"
    ingest.ingest(store.connect(path), [root.path])
    return create_app(lambda: QueryService(store.connect(path), "UTC"), lambda: NOW).test_client()


def test_json_event_rows_carry_time_and_labels(root, tmp_path):
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-30T01:00:00Z", effort="high"),
        req("m2", "2026-10-30T01:00:10Z", effort="medium")])
    rows = _client(root, tmp_path).get("/api/session/s1/timeline").get_json()["rows"]
    change = next(r for r in rows if r["kind"] == "setting_change")
    assert change["time"] == "2026-10-30 01:00:10" and "high" in change["label"] \
        and "medium" in change["label"]


def test_json_child_session_and_forked_from_rows_carry_labels_for_the_links(root, tmp_path):
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T01:00:00Z", output=M)],
                          prompt="parent prompt")
    root.write_transcript("p", "s9", [req("m1", "2026-10-29T01:00:00Z", output=M, forked_from="s1"),
                                      req("m9", "2026-10-29T05:00:00Z", output=M)])
    client = _client(root, tmp_path)
    fork = client.get("/api/session/s9/timeline").get_json()["rows"][0]
    assert (fork["kind"], fork["parent_session_id"], fork["parent_label"]) == (
        "forked_from", "s1", "parent prompt")
    parent = client.get("/api/session/s1/timeline").get_json()["rows"]
    child = next(r for r in parent if r["kind"] == "child_session")
    assert (child["session_id"], child["link_kind"]) == ("s9", "fork") and child["label"]


def test_the_session_page_has_no_events_table(root, tmp_path):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z")])
    html = _client(root, tmp_path).get("/session/s1").get_data(as_text=True)
    assert 'id="events"' not in html and "<h2>Events</h2>" not in html


def test_the_timeline_script_draws_request_flags_as_badges():
    from pathlib import Path
    js = (Path(__file__).parent.parent / "src/claude_cost/static/session.js").read_text()
    for label in ("Cold write", "Cold resume", "Oversized context"):
        assert label in js
