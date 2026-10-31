"""Seam 1: Spotlights (rules, pins, Auto-spotlights, closure, Forks, overlap) via the query service."""
from datetime import date

import pytest

from claude_cost.spotlights import SpotlightError
from helpers import req

M = 1_000_000  # output tokens = $20 on opus-5-5
KEY = r"PROJ-\d+"


def ticket_root(root):
    """s1: the ticket's main session (+ Subagent), s2: a Side session of it, s3: unrelated."""
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T01:00:00Z", output=M)],
                          branch="feature/proj-12-login", prompt="start work")
    root.write_subagent_transcript("p", "s1", "a1", [req("m2", "2026-10-29T02:00:00Z", output=M)],
                                   meta={"agentType": "Explore"})
    root.write_transcript("p", "s2", [req("m3", "2026-10-30T01:00:00Z", output=M)], prompt="side chat")
    root.write_transcript("p", "s3", [req("m4", "2026-10-30T02:00:00Z", output=M)], prompt="other")
    return root


def test_rule_spotlight_matches_each_field(root, service_for):
    root.write_transcript("p", "by-title", [req("m1", "2026-10-30T01:00:00Z", output=M)], title="Fix login")
    root.write_transcript("p", "by-branch", [req("m2", "2026-10-30T01:00:00Z", output=M)], branch="fix/login-bug")
    root.write_transcript("p", "by-cwd", [req("m3", "2026-10-30T01:00:00Z", output=M)],
                          cwd="/w/.claude/worktrees/login-x")
    root.write_transcript("p", "by-prompt", [req("m4", "2026-10-30T01:00:00Z", output=M)],
                          prompt="please fix LOGIN now")
    root.write_transcript("p", "no", [req("m5", "2026-10-30T01:00:00Z", output=M)], prompt="unrelated")
    svc = service_for(root)
    svc.save_spotlight("login", "login")
    d = svc.spotlight("login")
    assert {s.session_id for s in d.sessions} == {"by-title", "by-branch", "by-cwd", "by-prompt"}
    assert d.summary.kind == "rule" and d.summary.total == pytest.approx(80.0)


def test_first_prompt_skips_meta_and_wrappers(root, service_for):
    f = root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=M)])
    import json
    recs = [{"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta secret"}},
            {"type": "user", "message": {"role": "user", "content": "<command-name>/x</command-name>"}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "real ask"}]}}]
    f.write_text("\n".join(json.dumps(r) for r in recs) + "\n" + f.read_text())
    svc = service_for(root)
    svc.save_spotlight("a", "real ask")
    svc.save_spotlight("b", "secret")
    assert [s.session_id for s in svc.spotlight("a").sessions] == ["s1"]
    assert svc.spotlight("b").sessions == ()


def test_pinned_spotlight_and_unpin(root, service_for):
    ticket_root(root)
    svc = service_for(root)
    svc.pin_session("work", "s3")
    d = svc.spotlight("work")
    assert d.summary.kind == "pinned" and [s.session_id for s in d.sessions] == ["s3"]
    assert d.sessions[0].pinned and d.pins == ("s3",)
    svc.unpin_session("work", "s3")
    assert svc.spotlight("work").sessions == ()
    svc.delete_spotlight("work")
    with pytest.raises(KeyError):
        svc.spotlight("work")


def test_pins_survive_reingest_rebuild(root, tmp_path, service_for):
    from claude_cost import ingest, store
    ticket_root(root)
    svc = service_for(root)
    svc.pin_session("work", "s3")
    conn = store.connect(tmp_path / "store.sqlite")
    ingest.ingest(conn, [root.path], rebuild=True)
    from claude_cost.query import QueryService
    assert [s.session_id for s in QueryService(conn, "UTC").spotlight("work").sessions] == ["s3"]


def test_invalid_rule_is_rejected(root, service_for):
    svc = service_for(root)
    with pytest.raises(SpotlightError):
        svc.save_spotlight("bad", "(")
    with pytest.raises(SpotlightError):
        svc.save_spotlight("  ", "x")


def test_auto_spotlight_per_key_and_key_order(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=M)],
                          title="PROJ-3 and PROJ-1", branch="feat/proj-2", cwd="/w/PROJ-4",
                          prompt="about PROJ-5 and PROJ-1")
    root.write_transcript("p", "s2", [req("m2", "2026-10-30T01:00:00Z", output=M)], branch="x/PROJ-2")
    svc = service_for(root, key_pattern=KEY)
    # order: custom title, git branch, worktree path, first prompt; every key collected, no repeats
    assert svc.session_keys()["s1"] == ["PROJ-3", "PROJ-1", "PROJ-2", "PROJ-4", "PROJ-5"]
    names = {s.name: s for s in svc.spotlights()}
    assert set(names) == {"PROJ-1", "PROJ-2", "PROJ-3", "PROJ-4", "PROJ-5"}
    assert names["PROJ-2"].kind == "auto" and names["PROJ-2"].sessions == 2
    assert names["PROJ-2"].total == pytest.approx(40.0) and names["PROJ-3"].total == pytest.approx(20.0)


def test_no_pattern_means_no_auto_spotlights(root, service_for):
    ticket_root(root)
    assert service_for(root).spotlights() == []


def test_closure_includes_subagents_and_side_sessions(root, desktop, service_for):
    ticket_root(root)
    desktop.write_session("d1", "s1")
    desktop.write_session("d2", "s2", spawnedFrom={"sessionId": "local_d1", "sideSession": True})
    desktop.write_session("d3", "s3")
    svc = service_for(root, desktop=desktop, key_pattern=KEY)
    d = svc.spotlight("PROJ-12")
    assert {s.session_id for s in d.sessions} == {"s1", "s2"}  # s2 has no key, joins as a Side session
    assert d.summary.total == pytest.approx(60.0)  # s1 20 + its Subagent 20 + side 20
    s1 = next(s for s in d.sessions if s.session_id == "s1")
    assert s1.cost == pytest.approx(40.0) and s1.subagent_cost == pytest.approx(20.0)
    assert [(a.session_id, a.agent_id) for a in d.subagents] == [("s1", "a1")]
    assert next(s for s in d.sessions if s.session_id == "s2").link_kind == "side"
    assert svc.spotlight("PROJ-12").components["output"] == pytest.approx(60.0)


def test_side_session_of_side_session_is_included(root, desktop, service_for):
    ticket_root(root)
    desktop.write_session("d1", "s1")
    desktop.write_session("d2", "s2", spawnedFrom={"sessionId": "local_d1"})
    desktop.write_session("d3", "s3", spawnedFrom={"sessionId": "local_d2"})
    svc = service_for(root, desktop=desktop, key_pattern=KEY)
    assert {s.session_id for s in svc.spotlight("PROJ-12").sessions} == {"s1", "s2", "s3"}


def test_fork_excluded_unless_matched_or_pinned(root, desktop, service_for):
    ticket_root(root)
    desktop.write_session("d1", "s1")
    desktop.write_session("d3", "s3", forkedFromSessionId="local_d1")
    svc = service_for(root, desktop=desktop, key_pattern=KEY)
    d = svc.spotlight("PROJ-12")
    assert {s.session_id for s in d.sessions} == {"s1"}
    assert [(r.session_id, r.parent_id) for r in d.related] == [("s3", "s1")]
    assert d.related[0].cost == pytest.approx(20.0)
    assert d.summary.total == pytest.approx(40.0)  # the fork's 20 is not counted
    svc.pin_session("PROJ-12", "s3")  # pins attach to the Auto-spotlight
    d = svc.spotlight("PROJ-12")
    assert {s.session_id for s in d.sessions} == {"s1", "s3"} and d.related == ()


def test_fork_that_matches_the_rule_is_included(root, desktop, service_for):
    ticket_root(root)
    root.write_transcript("p", "s4", [req("m5", "2026-10-30T03:00:00Z", output=M)], branch="feature/proj-12-x")
    desktop.write_session("d1", "s1")
    desktop.write_session("d4", "s4", forkedFromSessionId="local_d1")
    svc = service_for(root, desktop=desktop, key_pattern=KEY)
    assert {s.session_id for s in svc.spotlight("PROJ-12").sessions} == {"s1", "s4"}


def test_cli_fork_by_copied_history_is_a_fork(root, service_for):
    """A transcript with forkedFrom records is a Fork even without the Desktop store."""
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T01:00:00Z", output=M)], branch="proj-12")
    root.write_transcript("p", "s9", [req("m1", "2026-10-29T01:00:00Z", output=M, forked_from="s1"),
                                      req("m9", "2026-10-30T01:00:00Z", output=M)], prompt="go on")
    svc = service_for(root, key_pattern=KEY)
    d = svc.spotlight("PROJ-12")
    assert [s.session_id for s in d.sessions] == ["s1"]
    assert [r.session_id for r in d.related] == ["s9"]
    assert d.summary.total == pytest.approx(20.0)  # the copied request stays with s1


def test_overlap_counts_fully_in_each_and_is_marked(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=M)], title="PROJ-1 PROJ-2")
    root.write_transcript("p", "s2", [req("m2", "2026-10-30T02:00:00Z", output=M)], title="PROJ-1")
    svc = service_for(root, key_pattern=KEY)
    one, two = svc.spotlight("PROJ-1"), svc.spotlight("PROJ-2")
    assert one.summary.total == pytest.approx(40.0) and two.summary.total == pytest.approx(20.0)
    assert {s.session_id: s.also_in for s in one.sessions} == {"s1": ("PROJ-2",), "s2": ()}
    assert [s.also_in for s in two.sessions] == [("PROJ-1",)]


def test_spotlight_page_data_lifetime_days_by_session_subagent_component(root, service_for):
    ticket_root(root)
    svc = service_for(root, key_pattern=KEY)
    d = svc.spotlight("PROJ-12")
    assert [x.day for x in d.days] == [date(2026, 10, 29)]
    day = d.days[0]
    assert day.cost == pytest.approx(40.0)
    assert day.sessions == {"s1": pytest.approx(40.0)} and day.subagent.total == pytest.approx(20.0)
    assert day.components["output"] == pytest.approx(40.0)
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T01:00:00Z", output=M),
                                      req("m6", "2026-10-31T01:00:00Z", output=M)],
                          branch="feature/proj-12-login", prompt="start work")
    d = service_for(root, key_pattern=KEY).spotlight("PROJ-12")
    assert [x.day.day for x in d.days] == [29, 30, 31] and d.days[1].cost == 0  # gap day kept


def test_top_spotlights_rank_by_period_spend_with_lifetime(root, service_for):
    from conftest import NOW
    root.write_transcript("p", "a", [req("m1", "2026-10-01T01:00:00Z", output=3 * M),
                                     req("m2", "2026-10-29T01:00:00Z", output=M)], title="PROJ-1")
    root.write_transcript("p", "b", [req("m3", "2026-10-29T01:00:00Z", output=2 * M)], title="PROJ-2")
    root.write_transcript("p", "c", [req("m4", "2026-10-01T01:00:00Z", output=5 * M)], title="PROJ-3")
    svc = service_for(root, key_pattern=KEY)
    top = svc.top_spotlights("last7", NOW)
    assert [t.summary.name for t in top] == ["PROJ-2", "PROJ-1"]  # PROJ-3 had nothing this period
    assert top[0].period_cost == pytest.approx(40.0) and top[1].period_cost == pytest.approx(20.0)
    assert top[1].summary.total == pytest.approx(80.0)  # lifetime
    assert (top[1].summary.first_activity, top[1].summary.last_activity) == (
        date(2026, 10, 1), date(2026, 10, 29))
    assert [t.summary.name for t in svc.top_spotlights("last7", NOW, top_n=1)] == ["PROJ-2"]


def test_top_spotlights_period_cost_is_less_than_lifetime_when_spend_spans_the_period_edge(
        root, service_for):
    from conftest import NOW
    root.write_transcript("p", "a", [req("m1", "2026-09-10T01:00:00Z", output=3 * M),
                                     req("m2", "2026-10-29T01:00:00Z", output=M)], title="PROJ-1")
    root.write_transcript("p", "b", [req("m3", "2026-10-05T01:00:00Z", output=2 * M),
                                     req("m4", "2026-10-30T01:00:00Z", output=M)], title="PROJ-1 again")
    (top,) = service_for(root, key_pattern=KEY).top_spotlights("last7", NOW)
    assert top.period_cost == pytest.approx(40.0)  # m2 and m4 only
    assert top.summary.total == pytest.approx(140.0)  # lifetime: all four Requests
    assert top.summary.requests == 4
    assert (top.summary.first_activity, top.summary.last_activity) == (
        date(2026, 9, 10), date(2026, 10, 30))


def test_comparison_of_several_spotlights(root, service_for):
    root.write_transcript("p", "a", [req("m1", "2026-10-29T01:00:00Z", output=M)], title="PROJ-1")
    root.write_transcript("p", "b", [req("m2", "2026-10-29T01:00:00Z", output=2 * M)], title="PROJ-2")
    svc = service_for(root, key_pattern=KEY)
    first, second = svc.spotlight_comparison(["PROJ-1", "PROJ-2"])
    assert (first.summary.total, second.summary.total) == (pytest.approx(20.0), pytest.approx(40.0))
    with pytest.raises(ValueError):
        svc.spotlight_comparison(["PROJ-1"])
    with pytest.raises(KeyError):
        svc.spotlight_comparison(["PROJ-1", "nope"])


def test_user_spotlight_overrides_auto_of_same_name(root, service_for):
    root.write_transcript("p", "a", [req("m1", "2026-10-29T01:00:00Z", output=M)], title="PROJ-1")
    root.write_transcript("p", "b", [req("m2", "2026-10-29T01:00:00Z", output=M)], title="other")
    svc = service_for(root, key_pattern=KEY)
    svc.save_spotlight("PROJ-1", "other")
    [s] = [s for s in svc.spotlights() if s.name == "PROJ-1"]
    assert s.kind == "rule" and s.sessions == 1


def test_fork_does_not_inherit_keys_from_copied_history_records(root, service_for):
    """Copied (forkedFrom) records carry the parent's branch/title/prompt: not the Fork's own."""
    import json
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T01:00:00Z", output=M)], branch="proj-12")
    copied = [
        json.dumps({"type": "custom-title", "customTitle": "PROJ-12 work", "sessionId": "s9",
                    "forkedFrom": {"sessionId": "s1"}}),
        json.dumps({"type": "user", "sessionId": "s9", "timestamp": "2026-10-01T00:00:00Z",
                    "gitBranch": "proj-12", "cwd": "/w/proj-12", "forkedFrom": {"sessionId": "s1"},
                    "message": {"role": "user", "content": "fix PROJ-12"}})]
    root.write_transcript("p", "s9", [req("m1", "2026-10-29T01:00:00Z", output=M, forked_from="s1"),
                                      req("m9", "2026-10-30T01:00:00Z", output=M)], extra_lines=copied)
    d = service_for(root, key_pattern=KEY).spotlight("PROJ-12")
    assert [s.session_id for s in d.sessions] == ["s1"]
    assert [r.session_id for r in d.related] == ["s9"]


def test_member_session_idle_in_the_period_counts_in_lifetime_figures_but_adds_no_period_spend(
        root, service_for):
    from conftest import NOW
    root.write_transcript("p", "a", [req("m1", "2026-10-29T01:00:00Z", output=M)])
    root.write_transcript("p", "idle", [req("m2", "2026-09-01T01:00:00Z", output=2 * M)])
    svc = service_for(root)
    svc.pin_session("work", "a")
    svc.pin_session("work", "idle")
    (top,) = svc.top_spotlights("last7", NOW)
    assert top.summary.name == "work" and top.period_cost == pytest.approx(20.0)
    assert (top.summary.sessions, top.summary.requests) == (2, 2)
    assert top.summary.total == pytest.approx(60.0)
    assert top.summary.first_activity == date(2026, 9, 1)


def test_spotlight_with_no_spend_in_the_period_is_left_out_of_the_ranking_only(root, service_for):
    from conftest import NOW
    root.write_transcript("p", "old", [req("m1", "2026-09-01T01:00:00Z", output=M)])
    root.write_transcript("p", "new", [req("m2", "2026-10-29T01:00:00Z", output=2 * M)])
    root.write_transcript("p", "quiet", [], prompt="talked, never called the model")
    svc = service_for(root)
    svc.pin_session("past", "old")
    svc.pin_session("present", "new")
    svc.pin_session("empty", "quiet")  # no Requests at all
    assert [t.summary.name for t in svc.top_spotlights("last7", NOW)] == ["present"]
    assert {s.name: s.total for s in svc.spotlights()} == {
        "past": pytest.approx(20.0), "present": pytest.approx(40.0), "empty": 0}


def _sub_day(root, service_for, subs):
    """One Session with a Subagent run per (agent id, type, output tokens) on one day."""
    root.write_transcript("p", "s1", [req("m0", "2026-10-29T01:00:00Z", output=M)], branch="proj-1")
    for i, (aid, typ, out) in enumerate(subs):
        root.write_subagent_transcript("p", "s1", aid, [req(f"x{i}", "2026-10-29T02:00:00Z", output=out)],
                                       meta={"agentType": typ} if typ else None)
    return service_for(root, key_pattern=KEY).spotlight("PROJ-1").days[0].subagent


def test_day_subagent_summary_total_runs_and_mostly(root, service_for):
    sub = _sub_day(root, service_for, [("a1", "Explore", M), ("a2", "general-purpose", 2 * M),
                                       ("a3", "Explore", M // 2)])
    assert sub.total == pytest.approx(70.0) and sub.runs == 3
    assert sub.top_type == "general-purpose" and not sub.all_same  # most cost, though fewest runs


def test_day_subagent_summary_all_same_type(root, service_for):
    sub = _sub_day(root, service_for, [("a1", "Explore", M), ("a2", "Explore", M)])
    assert (sub.runs, sub.top_type, sub.all_same) == (2, "Explore", True)


def test_day_subagent_summary_unknown_type(root, service_for):
    sub = _sub_day(root, service_for, [("a1", None, M)])
    assert (sub.runs, sub.top_type, sub.all_same) == (1, "unknown", True)


def test_day_without_subagent_spend_has_no_summary(root, service_for):
    root.write_transcript("p", "s1", [req("m0", "2026-10-29T01:00:00Z", output=M)], branch="proj-1")
    assert service_for(root, key_pattern=KEY).spotlight("PROJ-1").days[0].subagent is None
