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
    assert f.table.rows[0][0] == "big"


def test_used_here_unused_there(root, service_for):
    listing = [skill_listing_line("2026-10-30T09:59:00Z", _skill("tdd", 10_000))]
    use = tool_use_line("2026-10-30T10:06:00Z", "t1", "Skill", {"skill": "tdd"})
    _session(root, "proj-a", "a1", listing + [use])   # used in project a
    _session(root, "proj-b", "b1", listing)           # never used in project b
    f = _by_id(service_for(root).findings())["unused-listing"]
    assert [s.session_id for s in f.sessions] == ["b1"]
    assert f.avoidable == pytest.approx(0.004)
    (row,) = f.table.rows
    assert row[0] == "tdd" and row[2].items == ("proj-b: $0.00–$0.08, 1 session",) and row[3] == "1"


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
    assert [r[0] for r in f.table.rows] == ["mdn"]
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


def test_use_before_the_first_period_request_on_its_first_day_counts(root, service_for):
    # the Period starts at its first local day's start, not at its first Request (10:00)
    listing = [skill_listing_line("2026-10-30T09:59:00Z", _skill("tdd", 10_000))]
    early = tool_use_line("2026-10-30T08:00:00Z", "t0", "Skill", {"skill": "tdd"})
    _session(root, "p", "s1", listing)
    root.write_transcript("p", "early", [req("e1", "2026-10-30T10:20:00Z")], extra_lines=[early])
    day = __import__("datetime").date(2026, 10, 30)
    assert _by_id(service_for(root).findings(day, day))["unused-listing"].requests == ()


def test_use_after_the_period_does_not_count_on_a_past_period(root, service_for):
    import datetime
    listing = [skill_listing_line("2026-10-30T09:59:00Z", _skill("tdd", 10_000))]
    after = tool_use_line("2026-10-31T10:06:00Z", "t0", "Skill", {"skill": "tdd"})
    _session(root, "p", "s1", listing)
    root.write_transcript("p", "later", [req("l1", "2026-10-31T10:00:00Z", write_1h=1000)],
                          extra_lines=[after])
    svc = service_for(root)
    day = datetime.date(2026, 10, 30)
    assert _by_id(svc.findings(day, day))["unused-listing"].avoidable == pytest.approx(0.004)
    assert _by_id(svc.findings(day))["unused-listing"].requests == ()  # from day on: it was used
    assert _by_id(svc.findings())["unused-listing"].requests == ()  # all history: it was used


def test_used_elsewhere_ignores_a_use_in_another_project_after_the_period(root, service_for):
    import datetime
    listing = [skill_listing_line("2026-10-30T09:59:00Z", _skill("tdd", 10_000))]
    after = tool_use_line("2026-10-31T10:06:00Z", "t0", "Skill", {"skill": "tdd"})
    _session(root, "proj-a", "a1", listing)
    root.write_transcript("proj-b", "b1", [req("b-1", "2026-10-31T10:00:00Z", write_1h=1000)],
                          extra_lines=[after])
    day = datetime.date(2026, 10, 30)
    (row,) = _by_id(service_for(root).findings(day, day))["unused-listing"].table.rows
    assert row[3] == "0"


def test_used_elsewhere_counts_a_use_in_another_project_inside_the_period(root, service_for):
    import datetime
    listing = [skill_listing_line("2026-10-30T09:59:00Z", _skill("tdd", 10_000))]
    use = tool_use_line("2026-10-30T10:06:00Z", "t1", "Skill", {"skill": "tdd"})
    _session(root, "proj-a", "a1", listing + [use])
    _session(root, "proj-b", "b1", listing)
    day = datetime.date(2026, 10, 30)
    (row,) = _by_id(service_for(root).findings(day, day))["unused-listing"].table.rows
    assert row[3] == "1"


def test_current_period_counts_a_use_inside_it(root, service_for):
    listing = [skill_listing_line("2026-10-30T09:59:00Z", {**_skill("tdd", 10_000),
                                                           **_skill("big", 10_000)})]
    use = tool_use_line("2026-10-30T10:06:00Z", "t1", "Skill", {"skill": "tdd"})
    _session(root, "p", "s1", listing + [use])
    top = {f.id: f for f in service_for(root).top_findings("last7", NOW)}
    assert [r[0] for r in top["unused-listing"].table.rows] == ["big"]


def test_slash_command_use_after_the_period_does_not_count(root, service_for):
    import datetime
    import json
    from helpers import assistant_lines
    listing = [skill_listing_line("2026-10-30T09:59:00Z", _skill("plug:tdd", 10_000))]
    rec = assistant_lines(req("slash", "2026-10-31T10:07:00Z", output=10), "a1")[0]
    rec["attributionSkill"] = "tdd"
    _session(root, "proj-a", "a1", listing + [json.dumps(rec)])
    svc = service_for(root)
    day = datetime.date(2026, 10, 30)
    (row,) = _by_id(svc.findings(day, day))["unused-listing"].table.rows
    assert row[3] == "0"
    assert _by_id(svc.findings(day))["unused-listing"].requests == ()


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
    assert [r.message_id for r in f.requests if r.avoidable] == ["x2"]


def test_drains_command_lists_unused_listing(root, configure):
    _session(root, "p", "s1", [skill_listing_line("2026-10-30T09:59:00Z", _skill("big", 10_000))])
    out = io.StringIO()
    assert cli.main(["drains"], env=configure(root), now=NOW, out=out) == 0
    assert "Unused listing" in out.getvalue()


def test_drains_command_names_the_unused_listings(root, configure):
    listing = [skill_listing_line("2026-10-30T09:59:00Z", _skill("big", 10_000))]
    _session(root, "proj-a", "a1", listing)
    _session(root, "proj-b", "b1", listing)
    out = io.StringIO()
    cli.main(["drains"], env=configure(root), now=NOW, out=out)
    line = next(l for l in out.getvalue().splitlines() if "big" in l)
    assert "skill" in line and "2 projects" in line and "$" in line


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


def test_skill_used_via_attribution_skill_is_not_unused(root, service_for):
    """A skill invoked by slash command is recorded as the request's attributionSkill."""
    import json
    from helpers import assistant_lines
    rec = assistant_lines(req("slash", "2026-10-30T10:06:00Z", output=10), "s1")[0]
    rec["attributionSkill"] = "tdd"
    listing = [skill_listing_line("2026-10-30T09:59:00Z", _skill("tdd", 10_000)), json.dumps(rec)]
    _session(root, "p", "s1", listing)
    assert _by_id(service_for(root).findings())["unused-listing"].requests == ()


def test_listing_unused_in_two_projects_is_one_row(root, service_for):
    listing = [skill_listing_line("2026-10-30T09:59:00Z", _skill("tdd", 10_000))]
    use = tool_use_line("2026-10-30T10:06:00Z", "t1", "Skill", {"skill": "tdd"})
    _session(root, "proj-a", "a1", listing)
    _session(root, "proj-b", "b1", listing)
    _session(root, "proj-c", "c1", listing + [use])
    f = _by_id(service_for(root).findings())["unused-listing"]
    (row,) = f.table.rows
    assert row[:2] == ("tdd", "skill")
    assert row[2].summary == "2 projects" and row[2].items == ("proj-a: $0.00–$0.08, 1 session", "proj-b: $0.00–$0.08, 1 session")
    assert row[3] == "1"
    assert f.avoidable == pytest.approx(0.008)  # unchanged: per (project, listing)
    assert row[4] == "$0.01–$0.17"


def test_used_elsewhere_counts_invocations_under_the_unprefixed_skill_name(root, service_for):
    listing = [skill_listing_line("2026-10-30T09:59:00Z", _skill("plug:tdd", 10_000))]
    use = tool_use_line("2026-10-30T10:06:00Z", "t1", "Skill", {"skill": "tdd"})
    _session(root, "proj-a", "a1", listing)
    _session(root, "proj-b", "b1", listing + [use, use.replace("t1", "t2")])
    (row,) = _by_id(service_for(root).findings())["unused-listing"].table.rows
    assert row[0] == "plug:tdd" and row[3] == "2"


def test_projects_cell_lists_each_projects_cost_and_session_count(root, service_for):
    listing = [skill_listing_line("2026-10-30T09:59:00Z", _skill("tdd", 1_000_000))]
    _session(root, "proj-a", "a1", listing)
    _session(root, "proj-a", "a2", listing)
    _session(root, "proj-b", "b1", listing)
    (row,) = _by_id(service_for(root).findings())["unused-listing"].table.rows
    assert row[2].items == ("proj-a: $0.80–$16.80, 2 sessions", "proj-b: $0.40–$8.40, 1 session")


def test_detail_page_shows_table_not_run_on_text(root, configure):
    from claude_cost import ingest, store
    from claude_cost.query import QueryService
    from claude_cost.web import create_app
    listing = [skill_listing_line("2026-10-30T09:59:00Z", _skill("tdd", 10_000))]
    _session(root, "proj-a", "a1", listing)
    _session(root, "proj-b", "b1", listing)
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    client = create_app(factory, lambda: NOW).test_client()
    page = client.get("/findings/unused-listing").get_data(as_text=True)
    assert 'id="detail"' in page and "2 projects" in page and "<details>" in page
    assert ";" not in page.split('id="detail"')[1].split("</table>")[0]
    assert "sessions listed" not in client.get("/findings").get_data(as_text=True)


def test_per_request_amounts_with_overlapping_listings_models_and_removal(root, service_for):
    """Each request is charged every unused Listing it read, at its own model's read price."""
    srv = {"srv": "x" * 36_000}   # ~10,002 tokens with the "## srv" header
    root.write_transcript("p", "s1", [
        req("r1", "2026-10-30T10:00:00Z", write_1h=1000),                      # writes A
        req("r2", "2026-10-30T10:05:00Z", cache_read=1000),                    # A
        req("r3", "2026-10-30T10:07:00Z", model="claude-sonnet-5-5", cache_read=1000),  # A; writes srv
        req("r4", "2026-10-30T10:10:00Z", speed="fast", cache_read=1000),      # A + srv
        req("r5", "2026-10-30T10:15:00Z", cache_read=1000),                    # A (srv removed)
    ], extra_lines=[skill_listing_line("2026-10-30T09:59:00Z", _skill("a", 10_000)),
                    mcp_listing_line("2026-10-30T10:06:00Z", srv),
                    mcp_listing_line("2026-10-30T10:12:00Z", {}, uuid="rm", removed=("srv",))])
    f = _by_id(service_for(root).findings())["unused-listing"]
    got = {r.message_id: r.avoidable for r in f.requests if r.avoidable}
    # read per M: opus-5-5 $0.20, sonnet-5-5 $0.10, fast opus-5-5 x2 = $0.40
    assert got == {"r2": pytest.approx(0.002), "r3": pytest.approx(0.001),
                   "r4": pytest.approx((10_000 + 10_002) * 0.40 / 1e6, rel=1e-3),
                   "r5": pytest.approx(0.002)}


def test_used_elsewhere_counts_slash_command_uses(root, service_for):
    import json
    from helpers import assistant_lines
    listing = [skill_listing_line("2026-10-30T09:59:00Z", _skill("plug:tdd", 10_000))]
    rec = assistant_lines(req("slash", "2026-10-30T10:07:00Z", output=10), "b1")[0]
    rec["attributionSkill"] = "tdd"
    use = tool_use_line("2026-10-30T10:06:00Z", "t1", "Skill", {"skill": "tdd"})
    _session(root, "proj-a", "a1", listing)
    _session(root, "proj-b", "b1", listing + [use, json.dumps(rec)])
    (row,) = _by_id(service_for(root).findings())["unused-listing"].table.rows
    assert row[3] == "2"


def test_listing_never_read_after_it_was_written_is_not_counted(root, service_for):
    root.write_transcript("p", "s1", [req("s1-1", "2026-10-30T10:00:00Z", write_1h=1000)],
                          extra_lines=[skill_listing_line("2026-10-30T09:59:00Z",
                                                          _skill("big", 10_000))])
    f = _by_id(service_for(root).findings())["unused-listing"]
    assert f.table is None and f.detail == ""


def test_table_shows_top_25_then_one_row_for_the_rest(root, service_for):
    # skill sN costs N x $0.01: 2 later requests x N x 25K tokens x $0.20/M
    skills = {}
    for n in range(1, 28):
        skills |= _skill(f"s{n}", n * 25_000)
    _session(root, "p", "s1", [skill_listing_line("2026-10-30T09:59:00Z", skills)])
    f = _by_id(service_for(root).findings())["unused-listing"]
    assert f.detail == "27 unused listings in 1 project"
    assert len(f.table.rows) == 26
    assert f.table.rows[0][0] == "s27" and f.table.rows[24][0] == "s3"
    assert f.table.rows[25] == ("2 more listings", "", "", "", "$0.03–$0.63")


def test_listing_written_before_the_period_is_charged_to_its_active_streams_period_requests(
        root, service_for):
    root.write_transcript("p", "s1", [
        req("e1", "2026-10-20T10:00:00Z", write_1h=1000),
        req("p1", "2026-10-30T10:00:00Z", cache_read=1_000_000),
        req("p2", "2026-10-30T10:05:00Z", cache_read=1_000_000),
        req("p3", "2026-10-30T10:10:00Z", cache_read=1_000_000),
    ], extra_lines=[skill_listing_line("2026-10-20T09:59:00Z", _skill("old", 10_000))])
    # a Stream with no Request in the Period is not read at all
    root.write_transcript("p", "idle", [req("i1", "2026-10-20T10:00:00Z", write_1h=1000),
                                        req("i2", "2026-10-20T10:05:00Z", cache_read=1000)],
                          extra_lines=[skill_listing_line("2026-10-20T09:59:00Z", _skill("old2", 10_000))])
    since = __import__("datetime").date(2026, 10, 30)
    f = _by_id(service_for(root).findings(since))["unused-listing"]
    # e1 wrote it, so p1 reads it from the cache like p2 and p3: 3 x 10K x $0.20/M
    assert [r.message_id for r in f.requests] == ["p1", "p2", "p3"]
    assert f.avoidable == pytest.approx(0.006)
    assert [r[0] for r in f.table.rows] == ["old"]


def test_listing_written_by_the_first_period_request_is_not_charged_to_it(root, service_for):
    root.write_transcript("p", "s1", [
        req("e1", "2026-10-20T10:00:00Z", write_1h=1000),
        req("p1", "2026-10-30T10:00:00Z", write_1h=1000),
        req("p2", "2026-10-30T10:05:00Z", cache_read=1_000_000),
        req("p3", "2026-10-30T10:10:00Z", cache_read=1_000_000),
    ], extra_lines=[skill_listing_line("2026-10-30T09:59:00Z", _skill("new", 10_000))])
    since = __import__("datetime").date(2026, 10, 30)
    f = _by_id(service_for(root).findings(since))["unused-listing"]
    assert [r.message_id for r in f.requests if r.avoidable] == ["p2", "p3"]
    assert f.avoidable == pytest.approx(0.004)


def test_suffix_matching_agrees_with_the_endswith_rule_including_colons_in_the_prefix():
    from claude_cost.findings.unused_listing import _used

    def oracle(name, kind, used):
        return name in used or (kind == "skill" and any(name.endswith(f":{u}") for u in used))

    names = ["tdd", "plug:tdd", "a:b:tdd", "a:b:", "a::b", ":x", "x", "a:b"]
    useds = ["tdd", "b:tdd", "a:b:tdd", "plug", "a:b", "b", "", "x", "a", "::b", "plug:tdd"]
    for name in names:
        for u in useds:
            for kind in ("skill", "mcp"):
                assert _used(name, kind, {u}) == oracle(name, kind, {u}), (name, u, kind)
    assert _used("a:b:tdd", "skill", {"zzz", "b:tdd"})
    assert not _used("a:b:tdd", "mcp", {"b:tdd"})


def test_listing_in_a_resumed_sessions_second_transcript_is_attributed_to_that_session(
        root, desktop, service_for):
    root.write_transcript("p", "a-old", [req("o1", "2026-10-30T09:00:00Z", write_1h=1000)])
    root.write_transcript("p", "b-new", [
        req("n1", "2026-10-30T10:00:00Z", write_1h=1000),
        req("n2", "2026-10-30T10:05:00Z", cache_read=1_000_000),
        req("n3", "2026-10-30T10:10:00Z", cache_read=1_000_000),
    ], extra_lines=[skill_listing_line("2026-10-30T09:59:00Z", _skill("big", 10_000))])
    desktop.write_session("a", "b-new", priorCliSessionIds=["a-old"])
    f = _by_id(service_for(root, desktop=desktop).findings())["unused-listing"]
    assert [s.session_id for s in f.sessions] == ["a-old"]
    assert f.avoidable == pytest.approx(0.004)
    assert f.table.rows[0][2].items == ("p: $0.00–$0.08, 1 session",)


def test_detector_loads_the_session_graph_when_given_a_conn_without_one(root, service_for):
    from claude_cost import findings as findings_mod
    from claude_cost.findings import unused_listing
    _session(root, "p", "s1", [skill_listing_line("2026-10-30T09:59:00Z", _skill("big", 10_000))])
    svc = service_for(root)
    ctx = findings_mod.FindingContext(findings_mod.load_rows(svc._conn, None, None, None),
                                      {}, findings_mod.all_thresholds(), svc._table, svc._conn,
                                      median_first_context=0.0)
    assert unused_listing.detect(ctx).covered


def test_listing_in_one_transcript_is_charged_for_period_requests_in_the_sessions_other_transcript(
        root, desktop, service_for):
    root.write_transcript("p", "a-old", [req("o1", "2026-10-20T09:00:00Z", write_1h=1000)],
                          extra_lines=[skill_listing_line("2026-10-20T08:59:00Z", _skill("big", 10_000))])
    root.write_transcript("p", "b-new", [
        req("n1", "2026-10-30T10:00:00Z", cache_read=1_000_000),
        req("n2", "2026-10-30T10:05:00Z", cache_read=1_000_000),
    ])
    desktop.write_session("a", "b-new", priorCliSessionIds=["a-old"])
    since = __import__("datetime").date(2026, 10, 30)
    f = _by_id(service_for(root, desktop=desktop).findings(since))["unused-listing"]
    assert [s.session_id for s in f.sessions] == ["a-old"]
    assert {r.message_id for r in f.requests} <= {"n1", "n2"} and f.requests  # which one is #44
    assert f.avoidable > 0
    assert f.table.rows[0][0] == "big"


def test_a_reading_request_above_the_prompt_tier_threshold_pays_the_tier_on_its_reads(
        root, service_for):
    """claude-haiku-5-5 reads at $0.01/M, x5 above a 100K prompt; the 50K request stays flat."""
    haiku = "claude-haiku-5-5"
    root.write_transcript("p", "s1", [
        req("r1", "2026-10-30T10:00:00Z", model=haiku, write_1h=1000),
        req("r2", "2026-10-30T10:05:00Z", model=haiku, cache_read=150_000),
        req("r3", "2026-10-30T10:10:00Z", model=haiku, cache_read=50_000),
    ], extra_lines=[skill_listing_line("2026-10-30T09:59:00Z", _skill("a", 10_000))])
    f = _by_id(service_for(root).findings())["unused-listing"]
    got = {r.message_id: r.avoidable for r in f.requests if r.avoidable}
    assert got == {"r2": pytest.approx(10_000 * 0.05 / 1e6), "r3": pytest.approx(10_000 * 0.01 / 1e6)}


@pytest.mark.parametrize("input_, cache_read, tiered", [
    (0, 100_000, False),       # exactly the 100K threshold stays flat
    (0, 100_001, True),
    (30_000, 60_000, False),   # a 90K prompt: input counts once, not twice
    (50_000, 60_000, True),    # a 110K prompt only because of its input
])
def test_the_prompt_tier_is_decided_by_the_reading_requests_whole_prompt(
        root, service_for, input_, cache_read, tiered):
    haiku = "claude-haiku-5-5"
    root.write_transcript("p", "s1", [
        req("r1", "2026-10-30T10:00:00Z", model=haiku, write_1h=1000),
        req("r2", "2026-10-30T10:05:00Z", model=haiku, input=input_, cache_read=cache_read),
    ], extra_lines=[skill_listing_line("2026-10-30T09:59:00Z", _skill("a", 10_000))])
    f = _by_id(service_for(root).findings())["unused-listing"]
    rate = 0.05 if tiered else 0.01
    assert {r.message_id: r.avoidable for r in f.requests if r.avoidable} == {"r2": pytest.approx(10_000 * rate / 1e6)}


# --- avoidable spend as a range (#96): the writer's write raises only the upper bound ---------
# claude-opus-5-5 writes: 5m $5/M, 1h $8/M.

def _ranged(f):
    return {r.message_id: (r.avoidable, r.avoidable_high) for r in f.requests}


def test_writer_request_raises_only_the_upper_bound_by_the_listings_write(root, service_for):
    _session(root, "p", "s1", [skill_listing_line("2026-10-30T09:59:00Z", _skill("big", 10_000))])
    f = _by_id(service_for(root).findings())["unused-listing"]
    assert f.avoidable == pytest.approx(0.004)                    # the two reads, as before
    assert f.avoidable_high == pytest.approx(0.004 + 0.08)        # + 10K tokens x $8/M (1h write)
    got = _ranged(f)
    assert got["s1-1"] == (pytest.approx(0.0), pytest.approx(0.08))   # the writer, charged no read
    assert got["s1-2"] == (pytest.approx(0.002), pytest.approx(0.002))
    (s,) = f.sessions
    assert (s.avoidable, s.avoidable_high) == (pytest.approx(0.004), pytest.approx(0.084))


def test_write_price_is_the_writers_own_5m_1h_mix(root, service_for):
    root.write_transcript("p", "s1", [
        req("w", "2026-10-30T10:00:00Z", write_5m=500, write_1h=500),
        req("r", "2026-10-30T10:05:00Z", cache_read=1_000_000),
    ], extra_lines=[skill_listing_line("2026-10-30T09:59:00Z", _skill("a", 10_000))])
    f = _by_id(service_for(root).findings())["unused-listing"]
    assert _ranged(f)["w"][1] == pytest.approx(10_000 * 6.5 / 1e6)  # half $5/M, half $8/M


def test_write_price_includes_the_writers_prompt_tier(root, service_for):
    haiku = "claude-haiku-5-5"  # 1h write $0.20/M, x5 above a 100K prompt
    root.write_transcript("p", "s1", [
        req("w", "2026-10-30T10:00:00Z", model=haiku, write_1h=150_000),
        req("r", "2026-10-30T10:05:00Z", model=haiku, cache_read=50_000),
    ], extra_lines=[skill_listing_line("2026-10-30T09:59:00Z", _skill("a", 10_000))])
    f = _by_id(service_for(root).findings())["unused-listing"]
    assert _ranged(f)["w"][1] == pytest.approx(10_000 * 1.0 / 1e6)


def test_listing_written_before_the_period_has_no_write_so_the_bounds_are_equal(root, service_for):
    root.write_transcript("p", "s1", [
        req("e1", "2026-10-20T10:00:00Z", write_1h=1000),
        req("p1", "2026-10-30T10:00:00Z", cache_read=1_000_000),
        req("p2", "2026-10-30T10:05:00Z", cache_read=1_000_000),
    ], extra_lines=[skill_listing_line("2026-10-20T09:59:00Z", _skill("old", 10_000))])
    since = __import__("datetime").date(2026, 10, 30)
    f = _by_id(service_for(root).findings(since))["unused-listing"]
    assert f.avoidable_high == pytest.approx(f.avoidable) == pytest.approx(0.004)


def test_a_listing_never_read_adds_no_write_to_the_upper_bound(root, service_for):
    root.write_transcript("p", "s1", [req("w", "2026-10-30T10:00:00Z", write_1h=1000)],
                          extra_lines=[skill_listing_line("2026-10-30T09:59:00Z", _skill("a", 10_000))])
    assert _by_id(service_for(root).findings())["unused-listing"].requests == ()


def test_other_findings_have_no_upper_bound(root, service_for):
    _session(root, "p", "s1", [skill_listing_line("2026-10-30T09:59:00Z", _skill("big", 10_000))])
    others = [f for f in service_for(root).findings() if f.id != "unused-listing"]
    assert others and all(f.avoidable_high is None for f in others)
    assert all(r.avoidable_high is None for f in others for r in f.requests)


def test_detail_rows_and_their_cells_show_the_range_and_rank_by_the_lower_bound(root, service_for):
    # "wrote" has the higher upper bound (a Period writer), "old" the higher lower bound (3 reads)
    _session(root, "pa", "a1", [skill_listing_line("2026-10-30T09:59:00Z", _skill("wrote", 10_000))])
    root.write_transcript("pb", "b1", [
        req("e1", "2026-10-20T10:00:00Z", write_1h=1000),
        *[req(f"b{i}", f"2026-10-30T10:0{i}:00Z", cache_read=1_000_000) for i in range(3)],
    ], extra_lines=[skill_listing_line("2026-10-20T09:59:00Z", _skill("old", 10_000))])
    since = __import__("datetime").date(2026, 10, 30)
    f = _by_id(service_for(root).findings(since))["unused-listing"]
    assert [r[0] for r in f.table.rows] == ["old", "wrote"]
    assert f.table.rows[0][-1] == "$0.01"                # bounds equal: one value
    assert f.table.rows[1][-1] == "$0.00–$0.08"
    assert f.table.rows[1][2].items == ("pa: $0.00–$0.08, 1 session",)


def test_drains_command_and_pages_show_the_range(root, configure):
    from claude_cost import ingest, store
    from claude_cost.query import QueryService
    from claude_cost.web import create_app
    _session(root, "p", "s1", [skill_listing_line("2026-10-30T09:59:00Z", _skill("big", 10_000))])
    out = io.StringIO()
    cli.main(["drains"], env=configure(root), now=NOW, out=out)
    line = next(l for l in out.getvalue().splitlines() if "Unused listing" in l)
    assert "avoidable $0.00–$0.08" in line
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    client = create_app(factory, lambda: NOW).test_client()
    for url in ("/findings", "/findings/unused-listing", "/"):
        assert "$0.00–$0.08" in client.get(url).get_data(as_text=True), url
    page = client.get("/findings/unused-listing").get_data(as_text=True)
    assert "lower bounds" not in page and "lower bound" in page  # the note and formula name both


# --- avoidable spend as a range (#98): the Prompt tier drop raises the upper bound ------------
# claude-haiku-5-5: cache read $0.01/M, 1h write $0.20/M, output $0.50/M; all x5 above a 100K prompt.

HAIKU = "claude-haiku-5-5"


def _tier_drop_session(root, reader_model, reader_read, extra, **reader_kw):
    root.write_transcript("p", "s1", [
        req("w", "2026-10-30T10:00:00Z", model=HAIKU, write_1h=1000),
        req("r", "2026-10-30T10:05:00Z", model=reader_model, cache_read=reader_read, **reader_kw),
    ], extra_lines=extra)


def test_removing_the_unused_listings_drops_a_request_under_the_tier_so_the_upper_bound_adds_4_5_of_its_cost(
        root, service_for):
    """105K prompt, 95K without the 10K Listing: 4/5 of the request's whole cost goes away."""
    _tier_drop_session(root, HAIKU, 105_000,
                       [skill_listing_line("2026-10-30T09:59:00Z", _skill("a", 10_000))])
    f = _by_id(service_for(root).findings())["unused-listing"]
    cost = 105_000 * 0.05 / 1e6           # the request's whole cost, tiered
    low, high = _ranged(f)["r"]
    assert low == pytest.approx(10_000 * 0.05 / 1e6)
    assert high == pytest.approx(low + cost * 4 / 5)


@pytest.mark.parametrize("model, cache_read", [
    (HAIKU, 125_000),                  # still above 100K after the 10K Listing goes
    ("claude-opus-5-5", 105_000),      # untiered model
])
def test_no_tier_drop_when_the_request_stays_above_or_is_untiered(root, service_for, model, cache_read):
    _tier_drop_session(root, model, cache_read,
                       [skill_listing_line("2026-10-30T09:59:00Z", _skill("a", 10_000))])
    low, high = _ranged(_by_id(service_for(root).findings())["unused-listing"])["r"]
    assert high == pytest.approx(low)


def test_listings_sharing_a_tier_drop_split_it_by_token_share_and_the_request_counts_once(
        root, service_for):
    """125K prompt, 85K without the 10K + 30K Listings; the 10M output makes the cost visible."""
    _tier_drop_session(root, HAIKU, 125_000, [
        skill_listing_line("2026-10-30T09:59:00Z", {**_skill("a", 10_000), **_skill("b", 30_000)}),
    ], output=10_000_000)
    f = _by_id(service_for(root).findings())["unused-listing"]
    drop = (10_000_000 * 0.5 * 5 + 125_000 * 0.05) / 1e6 * 4 / 5
    writes = 40_000 * 0.2 / 1e6
    low, high = _ranged(f)["r"]
    assert low == pytest.approx(40_000 * 0.05 / 1e6)
    assert high == pytest.approx(low + drop)                       # counted once
    assert f.avoidable_high == pytest.approx(low + writes + drop)
    rows = {r[0]: r[4] for r in f.table.rows}
    assert rows == {"a": f"$0.00–${drop / 4 + 10_000 * 0.2 / 1e6:.2f}",
                    "b": f"$0.00–${drop * 3 / 4 + 30_000 * 0.2 / 1e6:.2f}"}
# --- Cold writes in the read range replace the read in the upper bound (#97) -------------------

def _cold_session(root, extra):
    root.write_transcript("p", "s1", [
        req("w", "2026-10-30T10:00:00Z", write_5m=1000),
        req("r1", "2026-10-30T10:04:00Z", cache_read=1_000_000),
        req("c", "2026-10-30T10:20:00Z", write_5m=1000),            # idle past the 5m TTL: Cold write
        req("r2", "2026-10-30T10:24:00Z", cache_read=1_000_000),
    ], extra_lines=extra)


def test_a_cold_write_in_the_read_range_replaces_its_read_in_the_upper_bound(root, service_for):
    _cold_session(root, [skill_listing_line("2026-10-30T09:59:00Z", _skill("big", 10_000))])
    f = _by_id(service_for(root).findings())["unused-listing"]
    assert f.avoidable == pytest.approx(0.006)                       # three reads, as before
    # writer's write + two reads + the Cold write in place of its read: 10K x ($5 + $5)/M + 2 x 10K x $0.2/M
    assert f.avoidable_high == pytest.approx(0.05 + 0.004 + 0.05)
    got = _ranged(f)
    assert got["c"] == (pytest.approx(0.002), pytest.approx(0.05))   # write, not read + write
    assert got["r1"] == (pytest.approx(0.002), pytest.approx(0.002))
    assert sum(h for _, h in got.values()) == pytest.approx(f.avoidable_high)


# --- integration of #97 and #98: the Listing's tokens in cache writes count toward the tier drop ----
def test_the_writers_cache_write_counts_the_listing_toward_the_tier_drop(root, service_for):
    """The writer's prompt is 105K, 95K without the 10K Listing it wrote: the tier drop applies to it."""
    root.write_transcript("p", "s1", [
        req("w", "2026-10-30T10:00:00Z", model=HAIKU, write_5m=105_000),
        req("r", "2026-10-30T10:04:00Z", model=HAIKU, cache_read=50_000),
    ], extra_lines=[skill_listing_line("2026-10-30T09:59:00Z", _skill("a", 10_000))])
    f = _by_id(service_for(root).findings())["unused-listing"]
    # 5m write $0.125/M x5 above 100K: the Listing's write 10K x $0.625/M, plus 4/5 of the 105K x $0.625/M cost
    assert _ranged(f)["w"] == (pytest.approx(0.0), pytest.approx(0.00625 + 0.0525))
    assert f.avoidable == pytest.approx(10_000 * 0.01 / 1e6)
    assert f.avoidable_high == pytest.approx(0.0001 + 0.00625 + 0.0525)
    assert f.table.rows[0][-1] == "$0.00–$0.06"


def test_a_cold_write_and_a_tier_drop_in_one_listings_range_sum_to_the_upper_bound(root, service_for):
    root.write_transcript("p", "s1", [
        req("w", "2026-10-30T10:00:00Z", model=HAIKU, write_5m=105_000),
        req("r1", "2026-10-30T10:04:00Z", model=HAIKU, cache_read=50_000),
        req("c", "2026-10-30T10:20:00Z", model=HAIKU, write_5m=105_000),   # Cold write, 105K prompt
        req("r2", "2026-10-30T10:24:00Z", model=HAIKU, cache_read=50_000),
    ], extra_lines=[skill_listing_line("2026-10-30T09:59:00Z", _skill("a", 10_000))])
    f = _by_id(service_for(root).findings())["unused-listing"]
    # lower: reads at $0.01/M (the Cold write's at the tiered $0.05/M); unchanged by writes and drops
    assert f.avoidable == pytest.approx(10_000 * (0.01 + 0.05 + 0.01) / 1e6)
    write, drop = 10_000 * 0.625 / 1e6, 105_000 * 0.625 / 1e6 * 4 / 5   # the Listing's write; 4/5 of the cost
    got = _ranged(f)
    assert got["w"] == (pytest.approx(0.0), pytest.approx(write + drop))
    assert got["c"] == (pytest.approx(10_000 * 0.05 / 1e6), pytest.approx(write + drop))
    assert got["r1"] == (pytest.approx(0.0001), pytest.approx(0.0001))
    assert f.avoidable_high == pytest.approx(2 * (write + drop) + 0.0002)
    assert sum(h for _, h in got.values()) == pytest.approx(f.avoidable_high)
    (row,) = f.table.rows
    assert row[-1] == "$0.00–$0.12" and row[2].items == ("p: $0.00–$0.12, 1 session",)
