"""Seam 1: Spotlights (Picked sessions, rule migration, Auto-spotlights, closure, Forks, overlap) via the query service."""
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


def legacy_rule_store(root, tmp_path, rules, pins=()):
    """A store as the rule-era code left it: rule Spotlights and pins, migration not yet run."""
    from claude_cost import ingest, store
    conn = store.connect(tmp_path / "store.sqlite")
    ingest.ingest(conn, [root.path])
    with conn:
        conn.executemany("INSERT INTO spotlights VALUES (?, ?)", rules)
        conn.executemany("INSERT INTO spotlight_pins VALUES (?, ?)", pins)
        conn.execute("DELETE FROM meta WHERE key = 'spotlight_picked'")
    return conn


def sprint_root(root):
    root.write_transcript("p", "by-title", [req("m1", "2026-10-30T01:00:00Z", output=M)], title="Fix login")
    root.write_transcript("p", "by-branch", [req("m2", "2026-10-30T01:00:00Z", output=M)], branch="fix/login-bug")
    root.write_transcript("p", "by-cwd", [req("m3", "2026-10-30T01:00:00Z", output=M)],
                          cwd="/w/.claude/worktrees/login-x")
    root.write_transcript("p", "by-prompt", [req("m4", "2026-10-30T01:00:00Z", output=M)],
                          prompt="please fix LOGIN now")
    root.write_transcript("p", "pinned", [req("m5", "2026-10-30T01:00:00Z", output=M)], prompt="elsewhere")
    root.write_transcript("p", "no", [req("m6", "2026-10-30T01:00:00Z", output=M)], prompt="unrelated")
    return root


def test_ingest_turns_a_rule_into_picked_sessions_keeping_total_and_sessions(root, tmp_path):
    from claude_cost import ingest
    from claude_cost.query import QueryService
    sprint_root(root)
    conn = legacy_rule_store(root, tmp_path, [("login", "login"), ("empty", "")],
                             [("login", "pinned")])
    ingest.ingest(conn, [root.path])
    svc = QueryService(conn, "UTC")
    d = svc.spotlight("login")
    assert {s.session_id for s in d.sessions} == {"by-title", "by-branch", "by-cwd", "by-prompt", "pinned"}
    assert d.summary.kind == "picked" and d.summary.total == pytest.approx(100.0)
    assert set(d.picked) == {s.session_id for s in d.sessions}
    assert svc.spotlight("empty").sessions == ()
    assert conn.execute("SELECT rule FROM spotlights WHERE name = 'login'").fetchone()[0] == ""


def test_a_rule_that_does_not_compile_is_skipped_and_left_untouched(root, tmp_path):
    from claude_cost import ingest
    from claude_cost.query import QueryService
    sprint_root(root)
    conn = legacy_rule_store(root, tmp_path, [("bad", "(unclosed"), ("login", "login")])
    ingest.ingest(conn, [root.path])
    svc = QueryService(conn, "UTC")
    assert svc.spotlight("bad").sessions == ()
    assert conn.execute("SELECT rule FROM spotlights WHERE name = 'bad'").fetchone()[0] == "(unclosed"
    assert {s.session_id for s in svc.spotlight("login").sessions} >= {"by-title"}
    assert conn.execute("SELECT rule FROM spotlights WHERE name = 'login'").fetchone()[0] == ""


def test_the_rule_migration_runs_once(root, tmp_path):
    from claude_cost import ingest
    from claude_cost.query import QueryService
    sprint_root(root)
    conn = legacy_rule_store(root, tmp_path, [("login", "login")])
    ingest.ingest(conn, [root.path])
    svc = QueryService(conn, "UTC")
    svc.remove_session("login", "by-title")
    root.write_transcript("p", "late", [req("m7", "2026-10-30T02:00:00Z", output=M)], title="login again")
    ingest.ingest(conn, [root.path])
    assert {s.session_id for s in QueryService(conn, "UTC").spotlight("login").sessions} == {
        "by-branch", "by-cwd", "by-prompt"}


def test_picked_sessions_survive_rebuild_and_migration_runs_after_sessions_exist(root, tmp_path):
    from claude_cost import ingest
    from claude_cost.query import QueryService
    sprint_root(root)
    conn = legacy_rule_store(root, tmp_path, [("login", "login")])
    ingest.ingest(conn, [root.path], rebuild=True)  # migration step on the rebuild path
    assert QueryService(conn, "UTC").spotlight("login").summary.total == pytest.approx(80.0)
    ingest.ingest(conn, [root.path], rebuild=True)  # and Picked sessions survive a later rebuild
    assert QueryService(conn, "UTC").spotlight("login").summary.total == pytest.approx(80.0)


def test_first_prompt_skips_meta_and_wrappers(root, service_for):
    f = root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=M)])
    import json
    recs = [{"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta secret"}},
            {"type": "user", "message": {"role": "user", "content": "<command-name>/x</command-name>"}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "real ask"}]}}]
    f.write_text("\n".join(json.dumps(r) for r in recs) + "\n" + f.read_text())
    assert service_for(root).session_labels()["s1"] == "real ask"


def test_picked_spotlight_and_remove(root, service_for):
    ticket_root(root)
    svc = service_for(root)
    svc.add_session("work", "s3")
    d = svc.spotlight("work")
    assert d.summary.kind == "picked" and [s.session_id for s in d.sessions] == ["s3"]
    assert d.sessions[0].picked and d.sessions[0].joined_as == "Picked" and d.picked == ("s3",)
    svc.remove_session("work", "s3")
    assert svc.spotlight("work").sessions == ()
    svc.delete_spotlight("work")
    with pytest.raises(KeyError):
        svc.spotlight("work")


def test_picked_sessions_survive_reingest_rebuild(root, tmp_path, service_for):
    from claude_cost import ingest, store
    ticket_root(root)
    svc = service_for(root)
    svc.add_session("work", "s3")
    conn = store.connect(tmp_path / "store.sqlite")
    ingest.ingest(conn, [root.path], rebuild=True)
    from claude_cost.query import QueryService
    assert [s.session_id for s in QueryService(conn, "UTC").spotlight("work").sessions] == ["s3"]


def test_creating_with_only_a_name_gives_an_empty_spotlight_and_with_ids_picks_them(root, service_for):
    ticket_root(root)
    svc = service_for(root)
    svc.create_spotlight("empty")
    d = svc.spotlight("empty")
    assert d.summary.kind == "picked" and d.sessions == () and d.summary.total == 0
    svc.create_spotlight("two", ["s1", "s3"])
    assert [s.session_id for s in svc.spotlight("two").sessions] == ["s1", "s3"]


def test_spotlight_needs_a_name(root, service_for):
    with pytest.raises(SpotlightError):
        service_for(root).create_spotlight("  ")


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
    assert next(s for s in d.sessions if s.session_id == "s2").joined_as == "Side session of start work"
    assert svc.spotlight("PROJ-12").components["output"] == pytest.approx(60.0)


def test_side_session_of_side_session_is_included(root, desktop, service_for):
    ticket_root(root)
    desktop.write_session("d1", "s1")
    desktop.write_session("d2", "s2", spawnedFrom={"sessionId": "local_d1"})
    desktop.write_session("d3", "s3", spawnedFrom={"sessionId": "local_d2"})
    svc = service_for(root, desktop=desktop, key_pattern=KEY)
    assert {s.session_id for s in svc.spotlight("PROJ-12").sessions} == {"s1", "s2", "s3"}


def test_fork_excluded_unless_picked(root, desktop, service_for):
    ticket_root(root)
    desktop.write_session("d1", "s1")
    desktop.write_session("d3", "s3", forkedFromSessionId="local_d1")
    svc = service_for(root, desktop=desktop, key_pattern=KEY)
    d = svc.spotlight("PROJ-12")
    assert {s.session_id for s in d.sessions} == {"s1"}
    assert [(r.session_id, r.parent_id) for r in d.related] == [("s3", "s1")]
    assert d.related[0].cost == pytest.approx(20.0)
    assert d.summary.total == pytest.approx(40.0)  # the fork's 20 is not counted
    svc.add_session("PROJ-12", "s3")  # Picked sessions attach to the Auto-spotlight
    d = svc.spotlight("PROJ-12")
    assert {s.session_id for s in d.sessions} == {"s1", "s3"} and d.related == ()


def test_fork_that_carries_the_key_is_included(root, desktop, service_for):
    ticket_root(root)
    root.write_transcript("p", "s4", [req("m5", "2026-10-30T03:00:00Z", output=M)], branch="feature/proj-12-x")
    desktop.write_session("d1", "s1")
    desktop.write_session("d4", "s4", forkedFromSessionId="local_d1")
    svc = service_for(root, desktop=desktop, key_pattern=KEY)
    d = svc.spotlight("PROJ-12")
    assert {s.session_id for s in d.sessions} == {"s1", "s4"}
    assert {s.session_id: s.joined_as for s in d.sessions} == {"s1": "Key match", "s4": "Key match"}


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
    with pytest.raises(SpotlightError, match="already exists"):  # no new one under an Auto key
        svc.create_spotlight("PROJ-1", ["b"])
    # one saved before the key appeared (or by the old rule migration) still wins
    svc._conn.execute("INSERT INTO spotlights (name) VALUES ('PROJ-1')")
    svc.add_sessions("PROJ-1", ["b"])
    [s] = [s for s in svc.spotlights() if s.name == "PROJ-1"]
    assert s.kind == "picked" and s.sessions == 1


def test_auto_spotlight_takes_extra_picked_sessions_and_key_match_cannot_be_removed(
        root, desktop, service_for):
    ticket_root(root)
    svc = service_for(root, key_pattern=KEY)
    svc.add_session("PROJ-12", "s3")
    d = svc.spotlight("PROJ-12")
    assert d.summary.kind == "auto"
    assert {s.session_id: s.joined_as for s in d.sessions} == {"s1": "Key match", "s3": "Picked"}
    by_id = {s.session_id: s for s in d.sessions}
    assert by_id["s1"].key_matched and not by_id["s1"].removable
    assert not by_id["s3"].key_matched and by_id["s3"].removable
    with pytest.raises(SpotlightError):
        svc.remove_session("PROJ-12", "s1")
    svc.remove_session("PROJ-12", "s3")
    assert [s.session_id for s in svc.spotlight("PROJ-12").sessions] == ["s1"]
    svc.add_session("PROJ-12", "s1")  # picked too: Key match takes precedence, still fixed
    s1 = svc.spotlight("PROJ-12").sessions[0]
    assert s1.joined_as == "Key match" and s1.picked
    assert s1.key_matched and not s1.removable
    with pytest.raises(SpotlightError):
        svc.remove_session("PROJ-12", "s1")


def test_removing_a_picked_session_removes_the_side_sessions_that_joined_through_it(
        root, desktop, service_for):
    ticket_root(root)
    desktop.write_session("d1", "s1")
    desktop.write_session("d2", "s2", spawnedFrom={"sessionId": "local_d1", "sideSession": True})
    svc = service_for(root, desktop=desktop)
    svc.add_session("work", "s1")
    d = svc.spotlight("work")
    assert {s.session_id: s.joined_as for s in d.sessions} == {
        "s1": "Picked", "s2": "Side session of start work"}
    svc.remove_session("work", "s1")
    assert svc.spotlight("work").sessions == ()


def test_a_picked_fork_is_marked_fork(root, desktop, service_for):
    ticket_root(root)
    desktop.write_session("d1", "s1")
    desktop.write_session("d3", "s3", forkedFromSessionId="local_d1")
    svc = service_for(root, desktop=desktop)
    svc.add_session("work", "s1")
    svc.add_session("work", "s3")
    assert {s.session_id: s.joined_as for s in svc.spotlight("work").sessions} == {
        "s1": "Picked", "s3": "Fork"}


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
    svc.add_session("work", "a")
    svc.add_session("work", "idle")
    (top,) = svc.top_spotlights("last7", NOW)
    assert top.summary.name == "work" and top.period_cost == pytest.approx(20.0)
    assert (top.summary.sessions, top.summary.requests) == (2, 2)
    assert top.summary.total == pytest.approx(60.0)
    assert top.summary.first_activity == date(2026, 9, 1)


def test_spotlight_with_no_spend_in_the_period_is_left_out_of_the_ranking_only(root, service_for):
    from conftest import NOW
    root.write_transcript("p", "old", [req("m1", "2026-09-01T01:00:00Z", output=M)])
    root.write_transcript("p", "new", [req("m2", "2026-10-29T01:00:00Z", output=2 * M)])
    svc = service_for(root)
    svc.add_session("past", "old")
    svc.add_session("present", "new")
    svc.create_spotlight("empty")  # no Sessions, so no Requests at all
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


# ---- Session search for the picker -------------------------------------------------------

def search_root(root, desktop):
    """w holds `base`; `fork-hi` ($40) and `fork-lo` ($10) are Forks of it; `bill` is unrelated."""
    def add(sid, prompt, out):
        root.write_transcript("p", sid, [req(f"m-{sid}", "2026-10-30T01:00:00Z", output=out)],
                              prompt=prompt)
    add("base", "login form", M)
    add("fork-lo", "aa flow", M // 2)
    add("fork-hi", "bb flow", 2 * M)
    add("bill", "billing page", M)
    desktop.write_session("d1", "base")
    desktop.write_session("d2", "fork-lo", forkedFromSessionId="local_d1")
    desktop.write_session("d3", "fork-hi", forkedFromSessionId="local_d1")


def picked_service(root, desktop, service_for):
    search_root(root, desktop)
    svc = service_for(root, desktop=desktop)
    svc.create_spotlight("w", ["base"])
    return svc


def test_search_with_an_empty_query_suggests_unpicked_forks_costliest_first(root, desktop, service_for):
    svc = picked_service(root, desktop, service_for)
    got = svc.search_sessions("", "w")
    assert [(c.session_id, c.kind, c.suggested) for c in got] == [
        ("fork-hi", "Fork", True), ("fork-lo", "Fork", True)]
    assert got[0].label == "bb flow" and got[0].cost == pytest.approx(40.0)


def test_search_matches_label_and_id_as_a_case_insensitive_subsequence(root, desktop, service_for):
    svc = picked_service(root, desktop, service_for)
    assert [c.session_id for c in svc.search_sessions("BLPG", "w")] == ["bill"]  # label: BiLling PaGe
    assert [c.session_id for c in svc.search_sessions("fkh", "w")] == ["fork-hi"]  # id: Fork-Hi
    assert svc.search_sessions("zzz", "w") == []
    assert svc.search_sessions("bb", "w")[0].kind == "Fork"


def test_search_does_not_look_in_branch_worktree_or_prompt_beyond_the_label(root, desktop, service_for):
    root.write_transcript("p", "x1", [req("mx", "2026-10-30T01:00:00Z", output=M)],
                          branch="feature/zebra", cwd="/work/zebra", prompt="start")
    svc = service_for(root, desktop=desktop)
    assert svc.search_sessions("zebra", "nothing") == []


def test_search_excludes_direct_members(root, desktop, service_for):
    svc = picked_service(root, desktop, service_for)
    assert "base" not in [c.session_id for c in svc.search_sessions("base", "w")]
    assert "base" in [c.session_id for c in svc.search_sessions("base", "other")]


def test_search_ranks_suggested_forks_then_match_quality_then_cost(root, desktop, service_for):
    svc = picked_service(root, desktop, service_for)
    got = svc.search_sessions("l", "w")
    # Forks first (costliest first), then the unrelated session.
    assert [c.session_id for c in got] == ["fork-hi", "fork-lo", "bill"]
    assert [c.suggested for c in got] == [True, True, False]


def test_search_prefers_contiguous_and_earlier_matches_over_cost(root, service_for):
    for sid, prompt, out in (("a", "x login", M * 3), ("b", "l-o-g-i-n", M * 2), ("c", "login", M)):
        root.write_transcript("p", sid, [req(f"m{sid}", "2026-10-30T01:00:00Z", output=out)], prompt=prompt)
    svc = service_for(root)
    assert [c.session_id for c in svc.search_sessions("login", "none")] == ["c", "a", "b"]


def test_search_returns_at_most_ten(root, service_for):
    for i in range(14):
        root.write_transcript("p", f"s{i:02}", [req(f"m{i}", "2026-10-30T01:00:00Z", output=M)],
                              prompt=f"task {i}")
    assert len(service_for(root).search_sessions("t", "none")) == 10


def test_a_key_cut_by_the_first_prompt_limit_is_ignored_but_one_before_the_cut_is_found(root, service_for):
    cut = "PROJ-1 " + "x" * 284 + " PROJ-8908"   # stored as ...PROJ-890 (300 characters)
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=M)], prompt=cut)
    root.write_transcript("p", "s2", [req("m2", "2026-10-30T01:00:00Z", output=M)],
                          title="PROJ-8908", prompt=cut)
    svc = service_for(root, key_pattern=KEY)
    keys = svc.session_keys()
    assert keys["s1"] == ["PROJ-1"]
    assert keys["s2"] == ["PROJ-8908", "PROJ-1"]   # title unaffected
    assert {s.name for s in svc.spotlights()} == {"PROJ-1", "PROJ-8908"}
