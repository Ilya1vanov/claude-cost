"""Seam 1: Advisor calls (ADR-0009) through a synthetic Data root, ingest and the query service."""
import json
from datetime import date

import pytest

from claude_cost import explore, ingest, pricing, store
from claude_cost.explore import ExploreSpec
from claude_cost.query import QueryService

from conftest import NOW
from helpers import Advisor, DataRoot, req

D30 = date(2026, 10, 30)
SONNET, OPUS = "claude-sonnet-5-5", "claude-opus-5-5"
# sonnet-5-5: $2 in / $10 out per M; opus-5-5: $4 in / $20 out per M, US-only inference x1.1
EXECUTOR = 0.022   # 1000 in + 2000 out on Sonnet
ADVISOR = 0.42     # 100K in + 1000 out on Opus
ADV = Advisor(model=OPUS, input=100_000, output=1000)


def advised(mid="m1", ts="2026-10-30T10:00:00Z", **kw):
    return req(mid, ts, model=SONNET, input=1000, output=2000, advisor=ADV, **kw)


def rows(svc, group, **filters):
    spec = ExploreSpec(group, {k: tuple(v) for k, v in filters.items()})
    return {r.value: r for r in svc.explore(spec).rows}


def test_advisor_call_is_daily_spend_at_the_advisors_price_and_one_more_request(root, service_for):
    root.write_transcript("p", "s1", [advised()])
    day = service_for(root).daily_spend(D30, D30)[0]
    assert (day.requests, day.cost) == (2, pytest.approx(EXECUTOR + ADVISOR))


def test_breakdowns_show_it_under_the_advisor_model_tool_and_unrecorded_effort(root, service_for):
    root.write_transcript("p", "s1", [advised(effort="high", tools=("Bash",))])
    svc = service_for(root)
    models = rows(svc, "model")
    assert models[OPUS].cost == pytest.approx(ADVISOR)
    assert models[SONNET].cost == pytest.approx(EXECUTOR)
    tools = rows(svc, "tool")
    assert tools["advisor"].cost == pytest.approx(ADVISOR) and tools["advisor"].requests == 1
    assert tools["Bash"].cost == pytest.approx(EXECUTOR)
    effort = rows(svc, "effort")
    assert effort["high"].cost == pytest.approx(EXECUTOR)
    assert effort[explore.NONE].cost == pytest.approx(ADVISOR)


def test_surface_and_branch_come_from_the_parent(root, service_for):
    root.write_transcript("p", "s1", [advised(entrypoint="claude-desktop", git_branch="feat")])
    svc = service_for(root)
    assert rows(svc, "surface")["Desktop"].cost == pytest.approx(EXECUTOR + ADVISOR)
    assert rows(svc, "git_branch")["feat"].cost == pytest.approx(EXECUTOR + ADVISOR)
    assert svc.explore(ExploreSpec("surface", {"surface": ("CLI",)})).total_requests == 0


def test_session_project_and_spotlight_cost_include_it_and_a_subagents_rolls_up(root, service_for):
    root.write_transcript("p", "s1", [advised()], branch="PROJ-1-work")
    root.write_subagent_transcript("p", "s1", "ag", [advised("sub", "2026-10-30T11:00:00Z")])
    svc = service_for(root, key_pattern=r"PROJ-\d+")
    (s,) = svc.session_summaries()
    assert (s.requests, s.cost) == (4, pytest.approx(2 * (EXECUTOR + ADVISOR)))
    assert (s.subagent_requests, s.subagent_cost) == (2, pytest.approx(EXECUTOR + ADVISOR))
    assert rows(svc, "project")["p"].cost == pytest.approx(2 * (EXECUTOR + ADVISOR))
    (sp,) = svc.spotlights()
    assert (sp.requests, sp.total) == (4, pytest.approx(2 * (EXECUTOR + ADVISOR)))


def test_a_request_copied_into_a_resumed_transcript_counts_its_advisor_call_once(root, service_for):
    r = advised(session_id="orig")
    root.write_transcript("p", "orig", [r])
    root.write_transcript("p", "resumed", [r])
    day = service_for(root).daily_spend(D30, D30)[0]
    assert (day.requests, day.cost) == (2, pytest.approx(EXECUTOR + ADVISOR))


def test_a_fork_copy_gives_the_advisor_call_to_the_original_session(root, service_for):
    root.write_transcript("p", "orig", [advised()])
    root.write_transcript("p", "fork1", [advised(forked_from="orig")])
    s = {x.session_id: x for x in service_for(root).session_summaries()}
    assert (s["orig"].requests, s["orig"].cost) == (2, pytest.approx(EXECUTOR + ADVISOR))


def test_ownership_recompute_moves_the_advisor_call_with_its_parent(root, tmp_path):
    root.write_transcript("p", "s1", [advised()])
    conn = store.connect(tmp_path / "s.sqlite")
    ingest.ingest(conn, [root.path])
    root.write_transcript("p", "s0", [advised(session_id="s1")])  # another copy of the same Request
    ingest.ingest(conn, [root.path])
    svc = QueryService(conn, "UTC")
    assert sum(s.requests for s in svc.session_summaries()) == 2
    assert svc.daily_spend(D30, D30)[0].cost == pytest.approx(EXECUTOR + ADVISOR)


def test_us_only_inference_of_the_parent_applies_but_its_fast_mode_does_not(root, service_for):
    root.write_transcript("p", "s1", [advised(inference_geo="us", speed="fast")])
    models = rows(service_for(root), "model")
    assert models[OPUS].cost == pytest.approx(ADVISOR * 1.1)
    assert models[SONNET].cost == pytest.approx(EXECUTOR * 2 * 1.1)


def test_prompt_tier_is_checked_against_the_advisor_calls_own_prompt(root, service_for):
    haiku = "claude-haiku-5-5"  # $0.10 in; x5 above 100K prompt tokens
    small = req("m1", "2026-10-30T10:00:00Z", model=OPUS, input=1000,
                advisor=Advisor(model=haiku, input=50_000))
    big = req("m2", "2026-10-30T11:00:00Z", model=OPUS, input=1000,
              advisor=Advisor(model=haiku, input=150_000))
    root.write_transcript("p", "s1", [small, big])
    assert rows(service_for(root), "model")[haiku].cost == pytest.approx(0.005 + 0.075)


def test_a_price_table_change_reprices_advisor_calls(root, tmp_path):
    root.write_transcript("p", "s1", [advised()])
    conn = store.connect(tmp_path / "s.sqlite")
    ingest.ingest(conn, [root.path])
    table = pricing.build_table({OPUS: {"input": 8, "output": 40}})
    ingest.ingest(conn, [root.path], price_table=table)
    svc = QueryService(conn, "UTC", price_table=table)
    assert rows(svc, "model")[OPUS].cost == pytest.approx(ADVISOR * 2)


def test_an_advisor_model_missing_from_the_price_table_is_unpriced(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T10:00:00Z", model=SONNET, output=10,
                                          advisor=Advisor(model="claude-future-9", input=5))])
    assert service_for(root).unpriced_models() == ["claude-future-9"]


def test_other_iteration_types_are_ignored(root, service_for):
    f = root.write_transcript("p", "s1", [req("m1", "2026-10-30T10:00:00Z", model=SONNET, output=10)])
    recs = [json.loads(line) for line in f.read_text().splitlines()]
    recs[0]["message"]["usage"]["iterations"] = [
        {"type": "message", "input_tokens": 0, "output_tokens": 10},
        {"type": "compaction", "model": OPUS, "input_tokens": 9999, "output_tokens": 9999}]
    f.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    assert service_for(root).daily_spend(D30, D30)[0].requests == 1


def test_transcripts_without_iterations_are_unchanged(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T10:00:00Z", model=SONNET, output=1_000_000)])
    day = service_for(root).daily_spend(D30, D30)[0]
    assert (day.requests, day.cost) == (1, pytest.approx(10.0))


def _stream_views(svc):
    t = svc.session_timeline("s1")
    return (svc.cache_events(), t.points, t.setting_changes, t.compactions, svc.compact_thresholds(),
            [(f.id, f.attributed, f.avoidable) for f in svc.findings()],
            (lambda u: (u.avg_concurrency, u.peak_concurrency))(
                svc.period_comparison("week", NOW).usage_current))


def _session(advisor):
    kw = dict(model=SONNET, advisor=advisor)
    return [req("a", "2026-10-30T10:00:00Z", write_1h=20_000, **kw),
            req("b", "2026-10-30T10:02:00Z", cache_read=20_000, write_1h=1_000, **kw),
            req("c", "2026-10-30T13:00:00Z", write_1h=150_000, **kw),   # Cold resume
            req("d", "2026-10-30T13:01:00Z", cache_read=150_000, effort="low", **kw)]


def _service(data_root, db):
    conn = store.connect(db)
    ingest.ingest(conn, [data_root.path])
    return QueryService(conn, "UTC")


def test_stream_readers_see_the_same_session_with_and_without_advisor_calls(tmp_path):
    plain, with_advisor = DataRoot(tmp_path / "plain"), DataRoot(tmp_path / "adv")
    plain.write_transcript("p", "s1", _session(None))
    with_advisor.write_transcript("p", "s1", _session(ADV))
    assert (_stream_views(_service(with_advisor, tmp_path / "a.sqlite"))
            == _stream_views(_service(plain, tmp_path / "b.sqlite")))


def test_a_store_from_before_advisor_calls_gets_them_at_the_next_ingest(root, tmp_path):
    root.write_transcript("p", "s1", [advised()])
    db = tmp_path / "old.sqlite"
    conn = store.connect(db)
    ingest.ingest(conn, [root.path])
    with conn:  # what a version-1 store looks like: no Advisor calls, no marker
        conn.execute("DELETE FROM requests WHERE is_advisor = 1")
        conn.execute("DELETE FROM meta WHERE key = 'advisor_calls'")
        conn.execute("PRAGMA user_version = 1")
    conn.close()
    conn = store.connect(db)  # migrates
    ingest.ingest(conn, [root.path])
    day = QueryService(conn, "UTC").daily_spend(D30, D30)[0]
    assert (day.requests, day.cost) == (2, pytest.approx(EXECUTOR + ADVISOR))


def _timeline(svc, agent_id=None):
    return svc.session_timeline("s1", agent_id, with_rows=True)


def test_timeline_lists_the_advisor_call_right_after_its_parent_with_model_and_cost(root, service_for):
    root.write_transcript("p", "s1", [advised("m1", "2026-10-30T10:00:00Z"),
                                      req("m2", "2026-10-30T10:01:00Z", model=SONNET, output=1)])
    t = _timeline(service_for(root))
    assert [r.kind for r in t.rows] == ["request", "advisor_call", "request"]
    parent, call, _ = t.rows
    assert (call.model, call.cost, call.ts) == (OPUS, pytest.approx(ADVISOR), parent.ts)
    assert call.parent_message_id == "m1"
    assert parent.cost == pytest.approx(EXECUTOR)


def test_timeline_total_includes_the_advisor_call_and_matches_the_session_cost(root, service_for):
    root.write_transcript("p", "s1", [advised()])
    svc = service_for(root)
    t = svc.session_timeline("s1")
    (s,) = svc.session_summaries()
    assert t.cost == pytest.approx(EXECUTOR + ADVISOR) == pytest.approx(s.cost)
    assert [p.cost for p in t.points] == [pytest.approx(EXECUTOR)]


def test_the_advisor_call_entry_is_not_a_stream_point_or_event(root, service_for):
    root.write_transcript("p", "s1", [advised("m1", "2026-10-30T10:00:00Z"),
                                      advised("m2", "2026-10-30T12:00:00Z")])
    t = _timeline(service_for(root))
    assert len(t.points) == 2 and t.setting_changes == ()
    assert [r.kind for r in t.rows] == ["request", "advisor_call", "idle_gap", "request", "advisor_call"]
    assert not {"context_tokens", "cache_event", "gap_seconds", "idle"} & set(vars(t.rows[1]))


def test_a_subagents_advisor_call_is_in_its_own_timeline_only(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T10:00:00Z", model=SONNET, output=1)])
    root.write_subagent_transcript("p", "s1", "ag", [advised("sub", "2026-10-30T11:00:00Z")])
    svc = service_for(root)
    assert "advisor_call" not in [r.kind for r in _timeline(svc).rows]
    sub = _timeline(svc, "ag")
    assert [r.kind for r in sub.rows] == ["request", "advisor_call"]
    assert sub.cost == pytest.approx(EXECUTOR + ADVISOR)


def test_json_advisor_call_row_carries_time_and_model_label(root, tmp_path):
    from claude_cost.web import create_app
    root.write_transcript("p", "s1", [advised()])
    path = tmp_path / "web.sqlite"
    ingest.ingest(store.connect(path), [root.path])
    client = create_app(lambda: QueryService(store.connect(path), "UTC"), lambda: NOW).test_client()
    row = client.get("/api/session/s1/timeline").get_json()["rows"][1]
    assert (row["kind"], row["time"]) == ("advisor_call", "2026-10-30 10:00:00")
    assert row["modelLabel"] and row["modelFamily"] == "opus"
