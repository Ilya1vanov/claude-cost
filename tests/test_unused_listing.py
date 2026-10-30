"""Unused listing Drain through the query service (seam 1) and the drains command (seam 2)."""
import io

import pytest

from claude_cost import cli

from conftest import NOW
from helpers import mcp_listing_line, req, skill_listing_line, tool_use_line

# claude-opus-5-5: cache read $0.20/M. Listing size = text length / 3.6 chars per token.


def _skill(name: str, tokens: int) -> dict[str, str]:
    """One skill whose listing line ("- name: desc") is `tokens` tokens (3.6 chars each)."""
    n = round(tokens * 3.6) - len(f"- {name}: ")
    return {name: "d" * n}


def _by_id(findings):
    return {f.id: f for f in findings}


def _session(root, project, sid, extra, reads=1_000_000):
    """A session with a first (writing) request and two later requests reading `reads` tokens."""
    root.write_transcript(project, sid, [
        req(f"{sid}-1", "2026-10-30T10:00:00Z", write_1h=1000),
        req(f"{sid}-2", "2026-10-30T10:05:00Z", cache_read=reads),
        req(f"{sid}-3", "2026-10-30T10:10:00Z", cache_read=reads),
    ], extra_lines=extra)


def test_unused_skill_avoidable_is_its_listing_read_cost(root, service_for):
    _session(root, "p", "s1", [skill_listing_line("2026-10-30T09:59:00Z", _skill("big", 10_000))])
    f = _by_id(service_for(root).findings())["unused-listing"]
    assert f.kind == "drain"
    # 10,000 tokens read by the two requests after the one that wrote it: 2 x 10K x $0.20/M
    assert f.avoidable == pytest.approx(0.004)
    assert "big" in f.detail


def test_used_here_unused_there(root, service_for):
    listing = [skill_listing_line("2026-10-30T09:59:00Z", _skill("tdd", 10_000))]
    use = tool_use_line("2026-10-30T10:06:00Z", "t1", "Skill", {"skill": "tdd"})
    _session(root, "proj-a", "a1", listing + [use])   # used in project a
    _session(root, "proj-b", "b1", listing)           # never used in project b
    f = _by_id(service_for(root).findings())["unused-listing"]
    assert [s.session_id for s in f.sessions] == ["b1"]
    assert f.avoidable == pytest.approx(0.004)
    assert "tdd in proj-b" in f.detail and "1 in other projects" in f.detail


def test_used_in_another_session_of_the_project_is_not_unused(root, service_for):
    listing = [skill_listing_line("2026-10-30T09:59:00Z", _skill("tdd", 10_000))]
    _session(root, "p", "s1", listing)
    _session(root, "p", "s2", listing + [tool_use_line("2026-10-30T10:06:00Z", "t1", "Skill",
                                                       {"skill": "tdd"})])
    assert _by_id(service_for(root).findings())["unused-listing"].requests == ()


def test_unused_mcp_server_matched_by_tool_calls_grouped_by_server(root, service_for):
    servers = {"claude.ai Figma": "i" * 3600, "mdn": "i" * 3600}  # 1,000 tokens each + header
    _session(root, "p", "s1", [
        mcp_listing_line("2026-10-30T09:59:00Z", servers),
        tool_use_line("2026-10-30T10:06:00Z", "t1", "mcp__claude_ai_Figma__get_design"),
    ])
    f = _by_id(service_for(root).findings())["unused-listing"]
    assert "mdn" in f.detail and "Figma" not in f.detail
    # 2 reads x ~1,002 tokens x $0.20/M
    assert f.avoidable == pytest.approx(2 * 1002 * 0.2 / 1e6, rel=0.01)


def test_use_outside_the_period_does_not_count(root, service_for):
    listing = [skill_listing_line("2026-10-20T09:59:00Z", _skill("tdd", 10_000))]
    root.write_transcript("p", "old", [req("o1", "2026-10-20T10:00:00Z", write_1h=1000)],
                          extra_lines=listing + [tool_use_line("2026-10-20T10:06:00Z", "t0",
                                                               "Skill", {"skill": "tdd"})])
    _session(root, "p", "s1", listing)
    svc = service_for(root)
    since = __import__("datetime").date(2026, 10, 30)
    assert _by_id(svc.findings(since))["unused-listing"].avoidable == pytest.approx(0.004)
    assert _by_id(svc.findings())["unused-listing"].requests == ()  # all history: it was used


def test_subagent_listing_is_read_only_by_that_subagents_requests(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T10:00:00Z", write_1h=1000),
                                      req("m2", "2026-10-30T10:05:00Z", cache_read=1000)])
    sub = root.write_subagent_transcript("p", "s1", "a1", [
        req("x1", "2026-10-30T10:01:00Z", write_1h=1000),
        req("x2", "2026-10-30T10:02:00Z", cache_read=1000),
    ], meta={"agentType": "Explore"})
    with sub.open("a") as fh:
        fh.write(skill_listing_line("2026-10-30T10:00:30Z", _skill("tdd", 10_000)) + "\n")
    f = _by_id(service_for(root).findings())["unused-listing"]
    assert [r.message_id for r in f.requests] == ["x2"]


def test_drains_command_lists_unused_listing(root, configure):
    _session(root, "p", "s1", [skill_listing_line("2026-10-30T09:59:00Z", _skill("big", 10_000))])
    out = io.StringIO()
    assert cli.main(["drains"], env=configure(root), now=NOW, out=out) == 0
    assert "Unused listing" in out.getvalue() and "skill big in p" in out.getvalue()


def test_a_removed_listing_stops_counting_from_the_removal(root, service_for):
    servers = {"srv": "x" * 36_000}   # 10,000 tokens
    root.write_transcript("p", "s1", [
        req("s1-1", "2026-10-30T10:00:00Z", write_1h=1000),
        req("s1-2", "2026-10-30T10:05:00Z", cache_read=1_000_000),
        req("s1-3", "2026-10-30T10:10:00Z", cache_read=1_000_000),
    ], extra_lines=[mcp_listing_line("2026-10-30T09:59:00Z", servers),
                    mcp_listing_line("2026-10-30T10:07:00Z", {}, uuid="rm", removed=("srv",))])
    f = _by_id(service_for(root).findings())["unused-listing"]
    # only the request before the removal read the listing: 1 x 10K x $0.20/M
    assert f.avoidable == pytest.approx(0.002, rel=1e-3)
