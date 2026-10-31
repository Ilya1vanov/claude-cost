"""Seam 1 (QueryService): Turns, the usage strip numbers and per-Turn component values."""
import pytest

from claude_cost import cli, ingest, store
from conftest import NOW
from helpers import (block_prompt_line, compact_summary_line, hook_injection_line, prompt_line, queued_prompt_line,
                     req, slash_command_line, tool_result_line)

D = "2026-10-29T"  # in this week; the previous week is 2026-10-19..25


def _cmp(svc):
    return svc.period_comparison("week", NOW)


def test_only_messages_the_user_typed_count_as_turns(root, service_for):
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z", output=1_000_000)], extra_lines=[
        prompt_line("u1", D + "09:00:00Z"),
        slash_command_line("u2", D + "09:10:00Z"),
        queued_prompt_line("u3", D + "09:20:00Z"),
        tool_result_line("u4", D + "09:30:00Z"),
        compact_summary_line("u5", D + "09:40:00Z"),
        hook_injection_line("u6", D + "09:50:00Z"),
    ])
    assert _cmp(service_for(root)).usage_current.turns == 3


def test_prompts_made_of_content_blocks_count_as_turns(root, service_for):
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z")], extra_lines=[
        block_prompt_line("u1", D + "09:00:00Z"),                # text plus image
        block_prompt_line("u2", D + "09:10:00Z", image=False),   # text blocks only
        tool_result_line("u3", D + "09:20:00Z"),                 # still not a prompt
        _blocks_line("u4", D + "09:30:00Z", "<system-reminder>x</system-reminder>"),
    ])
    assert _cmp(service_for(root)).usage_current.turns == 2


def test_a_prompt_with_an_injected_block_first_still_counts(root, service_for):
    from helpers import _user
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z")], extra_lines=[
        _user("u1", D + "09:00:00Z", [{"type": "text", "text": "<ide_opened_file>x</ide_opened_file>"},
                                      {"type": "text", "text": "fix the bug"}]),
        _user("u2", D + "09:10:00Z", [{"type": "text", "text": "<ide_opened_file>x</ide_opened_file>"}]),
    ])
    assert _cmp(service_for(root)).usage_current.turns == 1


def test_interrupt_markers_are_not_turns(root, service_for):
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z")], extra_lines=[
        prompt_line("u1", D + "09:00:00Z"),
        prompt_line("u2", D + "09:10:00Z", "[Request interrupted by user]"),
        prompt_line("u3", D + "09:20:00Z", "[Request interrupted by user for tool use]"),
    ])
    assert _cmp(service_for(root)).usage_current.turns == 1


def _blocks_line(uuid, ts, text):
    return block_prompt_line(uuid, ts, text, image=False)


def test_subagent_messages_are_not_turns(root, service_for):
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z")],
                          extra_lines=[prompt_line("u1", D + "09:00:00Z")])
    f = root.write_subagent_transcript("p", "s1", "ag", [req("b", D + "10:05:00Z")])
    f.write_text(f.read_text() + prompt_line("u2", D + "10:01:00Z") + "\n")
    assert _cmp(service_for(root)).usage_current.turns == 1


def test_resume_copies_do_not_double_count_turns(root, service_for):
    lines = [prompt_line("u1", D + "09:00:00Z", session_id="s1")]
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z")], extra_lines=lines)
    root.write_transcript("p", "s2", [req("b", D + "11:00:00Z", session_id="s1")],
                          extra_lines=lines + [prompt_line("u2", D + "10:30:00Z", session_id="s1")])
    assert _cmp(service_for(root)).usage_current.turns == 2


def test_turns_roll_up_to_the_logical_session(root, desktop, service_for):
    root.write_transcript("p", "cli-1", [req("a", D + "10:00:00Z")],
                          extra_lines=[prompt_line("u1", D + "09:00:00Z")])
    root.write_transcript("p", "cli-2", [req("b", D + "11:00:00Z")],
                          extra_lines=[prompt_line("u2", D + "10:30:00Z")])
    desktop.write_session("d1", "cli-2", priorCliSessionIds=["cli-1"])
    svc = service_for(root, desktop=desktop)
    assert _cmp(svc).usage_current.sessions == 1
    assert _cmp(svc).usage_current.turns == 2


def test_usage_counts_sessions_turns_and_requests_per_period(root, service_for):
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z"), req("b", D + "11:00:00Z")],
                          extra_lines=[prompt_line("u1", D + "09:00:00Z")])
    root.write_transcript("p", "s2", [req("c", "2026-10-20T10:00:00Z")],
                          extra_lines=[prompt_line("u2", "2026-10-20T09:00:00Z"),
                                       prompt_line("u3", "2026-10-20T09:30:00Z")])
    c = _cmp(service_for(root))
    assert (c.usage_current.sessions, c.usage_current.turns, c.usage_current.requests) == (1, 1, 2)
    assert (c.usage_baseline.sessions, c.usage_baseline.turns, c.usage_baseline.requests) == (1, 2, 1)


def test_per_turn_component_values_and_none_on_zero_turns(root, service_for):
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z", output=1_000_000)],  # $20 output
                          extra_lines=[prompt_line("u1", D + "09:00:00Z"),
                                       prompt_line("u2", D + "09:30:00Z")])
    root.write_transcript("p", "s2", [req("c", "2026-10-20T10:00:00Z", output=1_000_000)])
    out = {x.name: x for x in _cmp(service_for(root)).components}["output"]
    assert out.current_per_turn == pytest.approx(10)
    assert out.baseline_per_turn is None


def test_turns_survive_incremental_ingest_and_rebuild(root, tmp_path):
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z")],
                          extra_lines=[prompt_line("u1", D + "09:00:00Z")])
    conn = store.connect(tmp_path / "store.sqlite")
    count = lambda: conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    ingest.ingest(conn, [root.path])
    ingest.ingest(conn, [root.path])
    assert count() == 1
    root.append_to_transcript("p", "s1", [], raw=prompt_line("u2", D + "11:00:00Z") + "\n")
    ingest.ingest(conn, [root.path])
    assert count() == 2
    ingest.ingest(conn, [root.path], rebuild=True)
    assert count() == 2


def _dashboard(root, configure):
    import re  # noqa: F401
    from claude_cost.query import QueryService
    from claude_cost.web import create_app
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    return create_app(factory, lambda: NOW).test_client().get("/?period=week").get_data(as_text=True)


def test_dashboard_shows_usage_strip_above_cost_components_with_per_turn_columns(root, configure):
    import re
    root.write_transcript("p", "s1", [req("a", D + "10:00:00Z", output=1_000_000)], extra_lines=[
        prompt_line("u1", D + "09:00:00Z"), prompt_line("u2", D + "09:30:00Z")])
    html = _dashboard(root, configure)
    strip = re.search(r'id="usage-strip".*?</section>', html, re.S).group(0)
    for label in ("Sessions", "Turns", "Requests"):
        assert label in strip
    assert 'class="info" title="' in strip
    assert html.index('id="usage-strip"') < html.index("<h2>Cost components</h2>")
    comps = re.search(r"<h2>Cost components</h2>.*?</table>", html, re.S).group(0)
    assert "per Turn (this)" in comps and "per Turn (baseline)" in comps
    assert "$10.00" in comps          # $20 output over 2 Turns
    assert "n/a" in comps             # the baseline week has no Turns


def test_periods_before_turn_coverage_show_no_turn_figures(root, tmp_path):
    """A store that predates Turns is backfilled from the transcripts that still exist; a
    Request whose transcript was pruned has no Turns, so its period must not claim zero."""
    from claude_cost.query import QueryService
    old = root.write_transcript("p", "old", [req("o", "2026-10-20T10:00:00Z", output=1_000_000)],
                                extra_lines=[prompt_line("u0", "2026-10-20T09:00:00Z")])
    root.write_transcript("p", "s1", [req("a", "2026-10-26T00:00:00Z", output=1_000_000)],
                          extra_lines=[prompt_line("u1", "2026-10-26T00:00:00Z")])
    conn = store.connect(tmp_path / "store.sqlite")
    ingest.ingest(conn, [root.path])
    c = QueryService(conn, "UTC").period_comparison("week", NOW)
    assert c.usage_baseline.turns == 1            # fresh store: coverage is complete
    conn.execute("DELETE FROM meta WHERE key IN ('turns', 'turns_from')")
    conn.execute("DELETE FROM turns")
    old.unlink()
    ingest.ingest(conn, [root.path])
    c = QueryService(conn, "UTC").period_comparison("week", NOW)
    assert c.usage_baseline.requests == 1 and c.usage_baseline.turns is None
    assert c.usage_current.turns == 1
    out = {x.name: x for x in c.components}["output"]
    assert out.baseline_per_turn is None and out.current_per_turn == pytest.approx(20)
