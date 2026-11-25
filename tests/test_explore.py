"""Seam 1: Explore (filters and group-bys over every dimension) through the query service."""
import pytest

from claude_cost import explore
from claude_cost.explore import ExploreSpec

from helpers import req

# fixture model claude-opus-5-5: input $4/M, output $20/M, cache read $0.20/M, write 5m $5/M, 1h $8/M
M = 1_000_000


def _spec(group, **filters):
    return ExploreSpec(group, {k: tuple(v) for k, v in filters.items()})


def _rows(svc, group, **filters):
    return {r.value: r for r in svc.explore(_spec(group, **filters)).rows}


@pytest.fixture
def svc(root, service_for):
    root.write_transcript("proj-a", "s1", [
        req("m1", "2026-10-29T10:00:00Z", output=M, git_branch="main", effort="high",
            tools=("Bash", "Read"), skills=("tdd",)),
        req("m2", "2026-10-29T11:00:00Z", model="claude-sonnet-5", output=M, git_branch="feat",
            effort="low", tools=("Bash",), speed="fast", lines=2),
    ])
    root.write_transcript("proj-b", "s2", [
        req("m3", "2026-10-30T10:00:00Z", output=M // 2, entrypoint="claude-desktop"),
        req("m4", "2026-10-30T10:01:00Z", input=M, cache_read=M, write_1h=M // 2),
    ])
    root.write_subagent_transcript("proj-a", "s1", "ag1", [
        req("m5", "2026-10-29T12:00:00Z", output=M // 10),
    ], meta={"agentType": "Explore"})
    return service_for(root)


def test_total_matches_sum_of_requests_and_every_dimension_is_registered(svc):
    assert set(explore.DIMENSIONS) >= {
        "date", "data_root", "surface", "project", "git_branch", "session", "session_name", "subagent_type",
        "model", "effort", "speed", "component", "cache_event", "tool", "skill"}
    res = svc.explore(_spec("model"))
    assert res.total_requests == 5
    assert res.total_cost == pytest.approx(sum(r.cost for r in res.rows))


def test_group_by_scalar_dimensions(svc):
    assert _rows(svc, "project")["proj-a"].requests == 3
    assert _rows(svc, "project")["proj-b"].requests == 2
    assert _rows(svc, "surface")["Desktop"].requests == 1
    assert _rows(svc, "git_branch")["main"].cost == pytest.approx(20.0)
    assert _rows(svc, "git_branch")["(none)"].requests == 3
    assert _rows(svc, "effort")["high"].requests == 1 and _rows(svc, "effort")["low"].requests == 1
    sub = _rows(svc, "subagent_type")
    assert sub["Explore"].requests == 1 and sub["(main)"].requests == 4
    assert _rows(svc, "session")["s1"].requests == 3
    assert _rows(svc, "speed")["fast"].requests == 1 and _rows(svc, "speed")["standard"].requests == 4
    assert _rows(svc, "model")["claude-sonnet-5"].requests == 1


def test_group_by_date_lists_local_days_newest_first(svc):
    res = svc.explore(_spec("date"))
    assert [r.value for r in res.rows] == ["2026-10-30", "2026-10-29"]


def test_tool_and_skill_groups_overlap_and_are_flagged(svc):
    res = svc.explore(_spec("tool"))
    rows = {r.value: r for r in res.rows}
    assert res.overlapping
    assert rows["Bash"].requests == 2 and rows["Read"].requests == 1
    assert rows["Bash"].cost == pytest.approx(20.0 + _sonnet_cost(svc))
    assert rows["(none)"].requests == 3
    assert _rows(svc, "skill")["tdd"].requests == 1
    assert res.total_requests == 5  # distinct requests, not the sum of the groups


def _sonnet_cost(svc):
    return _rows(svc, "model")["claude-sonnet-5"].cost


def test_cost_component_and_cache_event_split_the_cost(svc):
    comp = _rows(svc, "component")
    assert comp["cache read"].cost == pytest.approx(0.2)
    assert comp["input"].cost == pytest.approx(4.0)
    assert comp["cache write (1h)"].cost == pytest.approx(4.0)
    total = svc.explore(_spec("component")).total_cost
    assert sum(r.cost for r in comp.values()) == pytest.approx(total)
    ev = _rows(svc, "cache_event")
    assert ev["warm write"].cost == pytest.approx(4.0)   # m4 is the session's first write: Warm
    assert ev["cache read"].cost == pytest.approx(0.2)
    assert sum(r.cost for r in ev.values()) == pytest.approx(total)


def test_filters_combine_and_can_be_multi_valued(svc):
    res = svc.explore(_spec("model", project=["proj-a"], git_branch=["main", "feat"]))
    assert res.total_requests == 2
    assert {r.value for r in res.rows} == {"claude-opus-5-5", "claude-sonnet-5"}
    assert svc.explore(_spec("model", project=["proj-b"], git_branch=["main"])).rows == ()


def test_filter_by_each_remaining_dimension(svc):
    assert svc.explore(_spec("project", tool=["Read"])).total_requests == 1
    assert svc.explore(_spec("project", skill=["tdd"])).total_requests == 1
    assert svc.explore(_spec("project", effort=["low"])).total_requests == 1
    assert svc.explore(_spec("project", speed=["fast"])).total_requests == 1
    assert svc.explore(_spec("project", surface=["Desktop"])).total_requests == 1
    assert svc.explore(_spec("project", session_name=["s2"])).total_requests == 2
    assert svc.explore(_spec("project", subagent_type=["Explore"])).total_requests == 1
    assert svc.explore(_spec("project", model=["claude-sonnet-5"])).total_requests == 1
    root_path = str(svc.explore(_spec("data_root")).rows[0].value)
    assert svc.explore(_spec("project", data_root=[root_path])).total_requests == 5


def test_date_filter_accepts_a_day_or_a_range(svc):
    assert svc.explore(_spec("model", date=["2026-10-30"])).total_requests == 2
    assert svc.explore(_spec("model", date=["2026-10-29..2026-10-29"])).total_requests == 3
    assert svc.explore(_spec("model", date=["2026-10-01..2026-10-31"])).total_requests == 5


def test_component_filter_restricts_the_cost_to_that_component(svc):
    res = svc.explore(_spec("model", component=["cache read"]))
    assert res.total_requests == 1 and res.total_cost == pytest.approx(0.2)
    res = svc.explore(_spec("component", cache_event=["warm write"]))
    assert {r.value for r in res.rows} == {"cache write (1h)"}


def test_tools_on_later_streamed_lines_are_unioned(root, service_for):
    root.write_transcript("p", "s", [req("m", "2026-10-30T10:00:00Z", output=10, lines=3,
                                          tools=("Bash",), skills=("x:y",))])
    svc = service_for(root)
    assert set(_rows(svc, "tool")) == {"Bash", "Skill"} and set(_rows(svc, "skill")) == {"x:y"}


def test_spec_url_round_trip_and_drill_down():
    spec = ExploreSpec.from_query([("group", "model"), ("project", "a"), ("project", "b"),
                                   ("tool", "mcp__x:y"), ("project", ""), ("page", "3")])
    assert spec.group == "model" and spec.filters == {"project": ("a", "b"), "tool": ("mcp__x:y",)}
    assert ExploreSpec.from_query(spec.to_query()) == spec
    d = spec.drill("model", "opus")
    assert d.filters["model"] == ("opus",) and d.filters["project"] == ("a", "b")
    assert spec.drill("project", "a").filters["project"] == ("a",)  # replaces, not adds
    assert spec.without("project", "a").filters["project"] == ("b",)
    with pytest.raises(ValueError):
        ExploreSpec.from_query([("group", "bogus")])
    with pytest.raises(ValueError):
        ExploreSpec.from_query([("group", "session")])  # a Session is listed, not a Breakdown
    assert ExploreSpec.from_query([("session", "s1")]).filters == {}  # the exact-id filter is gone
    assert ExploreSpec.from_query([]).group == "date"


def test_csv_of_a_breakdown(svc):
    csv_text = explore.to_csv(svc.explore(_spec("project")))
    lines = csv_text.strip().splitlines()
    assert lines[0] == "Project,Cost,Requests,Share %"
    assert lines[1].startswith("proj-a,") and len(lines) == 3


def test_dimension_registry_lets_other_tickets_plug_in(svc):
    class Big(explore.Dimension):
        key, label = "big", "Big"
        def load(self, conn):
            return {r["message_id"]: ["yes"] for r in conn.execute(
                "SELECT message_id FROM requests WHERE output_tokens >= 1000000")}
    explore.register(Big())
    try:
        assert _rows(svc, "big")["yes"].requests == 2
        assert svc.explore(_spec("model", big=["yes"])).total_requests == 2
    finally:
        del explore.DIMENSIONS["big"]


def test_drain_and_opportunity_dimensions_group_and_filter_by_finding(root, service_for):
    root.write_transcript("p", "s1", [
        req("f1", "2026-10-30T10:00:00Z", model="claude-fable-5", output=1_000_000),   # $50, Opportunity
        req("first", "2026-10-30T10:01:00Z", write_1h=20_000),
        req("resume", "2026-10-30T13:00:00Z", write_1h=150_000),                       # Cold resume Drain
        req("plain", "2026-10-30T13:01:00Z", cache_read=1_000),
    ])
    svc = service_for(root)
    assert {"drain", "opportunity"} <= set(explore.DIMENSIONS)
    drains = _rows(svc, "drain")
    assert drains["Cold resume"].requests == 1 and drains["Cold resume"].cost == pytest.approx(1.20)
    assert drains["(none)"].requests == 3
    assert _rows(svc, "opportunity")["Fable usage"].cost == pytest.approx(50.0)
    assert svc.explore(_spec("model", opportunity=["Fable usage"])).total_requests == 1
    assert svc.explore(_spec("model", drain=["Cold resume"])).total_requests == 1
    assert {o.value for o in svc.filter_options(_spec("drain"), "drain")} >= {"Cold resume"}


def test_a_request_computes_the_findings_once_although_two_finding_dimensions_are_used(root, service_for):
    root.write_transcript("p", "s1", [req("a", "2026-10-30T10:00:00Z", output=1_000)])
    svc = service_for(root)
    calls = []
    real = svc.findings
    svc.findings = lambda *a, **k: calls.append(1) or real(*a, **k)
    svc.explore(_spec("drain", opportunity=["Fable usage"]))
    assert calls == [1]  # one pipeline run although two finding dimensions are used


def test_cost_component_has_a_web_search_value(root, service_for):
    from helpers import web_search_result_line, web_search_use_line
    ts = "2026-10-29T10:00:00Z"
    root.write_transcript("proj-a", "s1", [req("m1", ts, output=M)], extra_lines=[
        web_search_use_line(ts, "tu1", "m1"), web_search_result_line(ts, "tu1", 3)])
    rows = _rows(service_for(root), "component")
    assert rows["web search"].cost == pytest.approx(0.03)
    assert rows["output"].cost == pytest.approx(20.0)
