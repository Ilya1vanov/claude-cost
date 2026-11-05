"""Seams 1 and 2: the Timeline's prompt and request rows, read from transcripts on demand."""
import pytest

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import (compact_summary_line, compact_line, prompt_line, req, tool_result_line)


def _session(root):
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-30T01:00:10Z", output=100, text="I will look.",
            tool_calls=(("Read", {"file_path": "/src/a.py"}),)),
        req("m2", "2026-10-30T01:00:20Z", output=100, text="Done reading."),
        req("m3", "2026-10-30T02:00:10Z", output=100, text="Second answer."),
    ], extra_lines=[
        prompt_line("u1", "2026-10-30T01:00:00Z", "fix the bug"),
        tool_result_line("r1", "2026-10-30T01:00:15Z"),
        prompt_line("u2", "2026-10-30T02:00:00Z", "now the tests"),
    ])


def test_rows_are_prompts_and_requests_grouped_into_turns_in_time_order(root, service_for):
    _session(root)
    rows = service_for(root).session_timeline("s1", with_rows=True).rows
    assert [(r.kind, getattr(r, "text", None)) for r in rows] == [
        ("prompt", "fix the bug"), ("request", "I will look."), ("request", "Done reading."),
        ("idle_gap", None), ("prompt", "now the tests"), ("request", "Second answer.")]


def test_request_row_carries_numbers_and_flags(root, service_for):
    _session(root)
    t = service_for(root).session_timeline("s1", with_rows=True)
    row = [r for r in t.rows if r.kind == "request"][2]
    point = t.points[2]
    assert (row.ts, row.model, row.context_tokens, row.cost) == (
        point.ts, point.model, point.context_tokens, point.cost)
    assert row.idle and row.gap_seconds == 3590 and row.content_available


def test_rows_are_left_out_unless_asked_for(root, service_for):
    _session(root)
    assert service_for(root).session_timeline("s1").rows == ()


@pytest.mark.parametrize("name, tool_input, summary", [
    ("Read", {"file_path": "/a/b.py"}, "/a/b.py"),
    ("Edit", {"file_path": "/a/c.py", "old_string": "x"}, "/a/c.py"),
    ("Write", {"file_path": "/a/d.py", "content": "y"}, "/a/d.py"),
    ("Bash", {"command": "ls -la\nrm x", "description": "list"}, "ls -la"),
    ("Grep", {"pattern": "def main", "path": "/a"}, "def main"),
    ("Glob", {"pattern": "**/*.py"}, "**/*.py"),
    ("MadeUpTool", {"file_path": "/a/e.py"}, ""),
    ("Read", {}, ""),
])
def test_tool_lines_summarise_the_main_argument_of_known_tools(root, service_for, name, tool_input,
                                                               summary):
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-30T01:00:00Z", output=1, tool_calls=((name, tool_input),))])
    (row,) = service_for(root).session_timeline("s1", with_rows=True).rows
    assert [(t.name, t.summary) for t in row.tools] == [(name, summary)]


def test_tool_line_carries_the_full_main_argument_for_expanding(root, service_for):
    command = "cat <<'X' > f\n" + "y" * 400 + "\nX"
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-30T01:00:00Z", output=1, tool_calls=(("Bash", {"command": command}),))])
    (row,) = service_for(root).session_timeline("s1", with_rows=True).rows
    (tool,) = row.tools
    assert tool.summary == "cat <<'X' > f" and tool.full == command


@pytest.mark.parametrize("raw, text", [
    ("<command-name>/reload-plugins</command-name>\n<command-message>reload-plugins</command-message>\n"
     "<command-args></command-args>", "/reload-plugins"),
    ("<command-message>review</command-message>\n<command-name>/review</command-name>\n"
     "<command-args>78 --strict</command-args>", "/review 78 --strict"),
])
def test_slash_command_prompt_is_shown_as_the_command_without_tags(root, service_for, raw, text):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:10Z", output=1)],
                          extra_lines=[prompt_line("u1", "2026-10-30T01:00:00Z", raw),
                                       prompt_line("u2", "2026-10-30T01:00:05Z", "plain")])
    prompts = [r for r in service_for(root).session_timeline("s1", with_rows=True).rows
               if r.kind == "prompt"]
    assert [(p.text, p.command) for p in prompts] == [(text, True), ("plain", False)]


def test_text_and_tools_of_a_streamed_request_are_joined_across_its_lines(root, service_for):
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-30T01:00:00Z", output=1, lines=2, text="Reading it.",
            tool_calls=(("Read", {"file_path": "/a"}), ("Bash", {"command": "ls"})))])
    (row,) = service_for(root).session_timeline("s1", with_rows=True).rows
    assert row.text == "Reading it."
    assert [t.name for t in row.tools] == ["Read", "Bash"]


def test_only_typed_prompts_are_rows(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:10Z", output=1)], extra_lines=[
        prompt_line("u1", "2026-10-30T01:00:00Z", "typed"),
        tool_result_line("r1", "2026-10-30T01:00:05Z"),
        compact_line("c1", "2026-10-30T01:00:06Z", 10, 5),
        compact_summary_line("cs1", "2026-10-30T01:00:07Z")])
    rows = service_for(root).session_timeline("s1", with_rows=True).rows
    assert [r.kind for r in rows] == ["prompt", "compaction", "request"]
    assert rows[1].summary == ""  # the summary record is not linked to the boundary here


def test_a_request_whose_transcript_is_gone_keeps_its_numbers(root, service_for):
    f = root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:10Z", output=1_000_000)],
                              extra_lines=[prompt_line("u1", "2026-10-30T01:00:00Z", "hi")])
    service = service_for(root)
    f.unlink()
    (row,) = service.session_timeline("s1", with_rows=True).rows
    assert row.kind == "request" and not row.content_available
    assert row.text == "" and row.tools == () and row.cost > 0 and row.context_tokens == 0


def test_a_surviving_copy_supplies_the_text_when_the_owners_transcript_is_gone(root, service_for):
    owner = root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:10Z", text="original")])
    root.write_transcript("p", "s2", [req("m1", "2026-10-30T01:00:10Z", text="original",
                                          session_id="s1", forked_from="s1")])
    service = service_for(root)
    owner.unlink()
    (row,) = service.session_timeline("s1", with_rows=True).rows
    assert row.content_available and row.text == "original"


def test_subagent_timeline_has_its_own_request_rows_and_no_prompts(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:10Z")],
                          extra_lines=[prompt_line("u1", "2026-10-30T01:00:00Z", "go")])
    root.write_subagent_transcript("p", "s1", "a1", [
        req("sa1", "2026-10-30T01:00:30Z", text="agent says", tool_calls=(("Bash", {"command": "pwd"}),))],
        meta={"agentType": "Explore"})
    rows = service_for(root).session_timeline("s1", "a1", with_rows=True).rows
    assert [(r.kind, r.text) for r in rows] == [("request", "agent says")]


def _client(root, configure):
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    return create_app(factory, lambda: NOW).test_client()


def test_json_endpoint_returns_the_rows_tagged_by_kind(root, configure):
    _session(root)
    client = _client(root, configure)
    resp = client.get("/api/session/s1/timeline")
    assert resp.status_code == 200
    rows = resp.get_json()["rows"]
    assert [r["kind"] for r in rows] == ["prompt", "request", "request", "idle_gap", "prompt",
                                         "request"]
    assert rows[0]["text"] == "fix the bug" and rows[0]["time"] == "2026-10-30 01:00:00"
    first = rows[1]
    assert first["text"] == "I will look." and first["tools"] == [{"name": "Read", "summary": "/src/a.py", "full": "/src/a.py"}]
    assert first["time"] == "2026-10-30 01:00:10" and first["modelLabel"] == "Opus 5.5"
    assert set(first) >= {"cost", "context_tokens", "idle", "cold_write", "cold_resume",
                          "oversized", "content_available", "effort"}


def test_json_endpoint_serves_a_subagent_and_404s_for_unknown_ids(root, configure):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:10Z")])
    root.write_subagent_transcript("p", "s1", "a1", [req("sa1", "2026-10-30T01:00:30Z", text="hi")])
    client = _client(root, configure)
    assert client.get("/api/session/s1/agent/a1/timeline").get_json()["rows"][0]["text"] == "hi"
    assert client.get("/api/session/nope/timeline").status_code == 404
    assert client.get("/api/session/s1/agent/nope/timeline").status_code == 404


def test_session_page_has_a_timeline_box_pointing_at_the_endpoint_and_no_requests_table(
        root, configure):
    _session(root)
    html = _client(root, configure).get("/session/s1").get_data(as_text=True)
    assert 'id="timeline-box"' in html and 'data-src="/api/session/s1/timeline"' in html
    assert 'id="requests"' not in html


def test_a_transcript_line_that_is_not_an_object_does_not_break_summary_reading(tmp_path):
    from claude_cost import timeline_rows
    path = tmp_path / "t.jsonl"
    path.write_text('["isCompactSummary"]\n')
    assert timeline_rows.read_summaries([str(path)], {"c1"}) == {}
