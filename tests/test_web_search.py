"""The web search fee: $0.01 per recorded search, charged to the Request that issued the call."""
from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from claude_cost import ingest, pricing, store
from claude_cost.query import QueryService
from helpers import req, tool_use_line, web_search_result_line, web_search_use_line

DAY = date(2026, 10, 30)
TS = "2026-10-30T01:00:00Z"
TS2 = "2026-10-30T01:00:05Z"


def total(svc) -> float:
    return svc.daily_spend(DAY, DAY)[0].cost


def _one(root, service_for, *lines, **req_kw):
    root.write_transcript("p", "s1", [req("m1", TS, **req_kw)], extra_lines=list(lines))
    return service_for(root)


def test_a_search_count_of_two_costs_two_cents(root, service_for):
    svc = _one(root, service_for, web_search_use_line(TS, "tu1", "m1"),
               web_search_result_line(TS2, "tu1", 2))
    assert total(svc) == pytest.approx(0.02)


def test_the_fee_is_added_to_the_token_cost(root, service_for):
    svc = _one(root, service_for, web_search_use_line(TS, "tu1", "m1"),
               web_search_result_line(TS2, "tu1", 1), output=1_000_000)
    assert total(svc) == pytest.approx(20.01)
    assert [s.cost for s in svc.session_summaries()] == [pytest.approx(20.01)]


def test_a_result_without_a_search_count_counts_as_one_search(root, service_for):
    svc = _one(root, service_for,
               web_search_use_line(TS, "tu1", "m1"), web_search_result_line(TS2, "tu1", None))
    assert total(svc) == pytest.approx(0.01)


def test_an_error_result_costs_nothing(root, service_for):
    svc = _one(root, service_for,
               web_search_use_line(TS, "tu1", "m1"), web_search_result_line(TS2, "tu1", error=True))
    assert total(svc) == 0


def test_a_call_with_no_result_line_costs_nothing(root, service_for):
    assert total(_one(root, service_for, web_search_use_line(TS, "tu1", "m1"))) == 0


def test_an_agent_results_tool_stats_search_count_is_not_a_web_search(root, service_for):
    result = {"type": "user", "uuid": "r-ag1", "timestamp": TS2,
              "toolUseResult": {"status": "completed", "toolStats": {"searchCount": 4}},
              "message": {"role": "user", "content": [
                  {"type": "tool_result", "tool_use_id": "ag1", "content": "done"}]}}
    svc = _one(root, service_for, tool_use_line(TS, "ag1", "Agent", {"prompt": "p"}, "m1"),
               json.dumps(result))
    assert total(svc) == 0


def test_only_websearch_results_are_counted(root, service_for):
    other = web_search_result_line(TS2, "tu9", 3)  # a result whose call was not WebSearch
    assert total(_one(root, service_for, other, output=1_000_000)) == pytest.approx(20.0)


def test_several_calls_of_one_request_add_up_across_streamed_lines(root, service_for):
    svc = _one(root, service_for,
               web_search_use_line(TS, "tu1", "m1"), web_search_use_line(TS, "tu2", "m1"),
               web_search_result_line(TS2, "tu1", 1), web_search_result_line(TS2, "tu2", 2),
               lines=2)
    assert total(svc) == pytest.approx(0.03)


def test_a_result_replayed_in_resumed_transcripts_counts_once(root, service_for):
    uses = [web_search_use_line(TS, "tu1", "m1"), web_search_result_line(TS2, "tu1", 2)]
    root.write_transcript("p", "s1", [req("m1", TS)], extra_lines=uses + uses)
    root.write_transcript("p", "s2", [req("m1", TS, session_id="s1")], extra_lines=uses)
    svc = service_for(root)
    assert (total(svc), len(svc.session_summaries())) == (pytest.approx(0.02), 1)


def test_a_result_arriving_in_a_later_ingest_is_charged_then(root, service_for):
    root.write_transcript("p", "s1", [req("m1", TS)],
                          extra_lines=[web_search_use_line(TS, "tu1", "m1")])
    assert total(service_for(root)) == 0
    root.append_to_transcript("p", "s1", [], raw=web_search_result_line(TS2, "tu1", 2) + "\n")
    assert total(service_for(root)) == pytest.approx(0.02)


def test_a_search_in_a_subagent_counts_toward_it_and_its_session(root, service_for):
    root.write_transcript("p", "s1", [req("m0", TS)])
    root.write_subagent_transcript("p", "s1", "a1", [req("m1", TS)], extra_lines=[
        web_search_use_line(TS, "tu1", "m1"), web_search_result_line(TS2, "tu1", 2)])
    svc = service_for(root)
    assert [s.cost for s in svc.subagent_summaries()] == [pytest.approx(0.02)]
    assert [s.cost for s in svc.session_summaries()] == [pytest.approx(0.02)]


def test_a_forks_copied_request_keeps_its_fee_with_the_parent(root, service_for):
    uses = [web_search_use_line(TS, "tu1", "m1"), web_search_result_line(TS2, "tu1", 2)]
    root.write_transcript("p", "fork", [req("m1", TS, forked_from="orig")], extra_lines=uses)
    root.write_transcript("p", "orig", [req("m1", TS)], extra_lines=uses)
    svc = service_for(root)
    assert total(svc) == pytest.approx(0.02)
    assert {s.session_id: s.cost for s in svc.session_summaries()} == {"orig": pytest.approx(0.02)}


def test_the_fable_usage_finding_never_reports_the_search_fee_as_avoidable(root, service_for):
    root.write_transcript("p", "s1", [req("f", TS, model="claude-fable-5", input=1_000_000)],
                          extra_lines=[web_search_use_line(TS, "tu1", "f"),
                                       web_search_result_line(TS2, "tu1", 3)])
    f = {x.id: x for x in service_for(root).findings()}["fable-usage"]
    assert f.attributed == pytest.approx(10.03)
    assert f.avoidable == pytest.approx(10 - 4)  # tokens only: Fable $10 vs Opus 5.5 $4


def _fee_lines(mid: str = "m1"):
    return [web_search_use_line(TS, "tu1", mid), web_search_result_line(TS2, "tu1", 2)]


def test_the_per_search_rate_is_dated(root, service_for, monkeypatch):
    monkeypatch.setattr(pricing, "WEB_SEARCH_FEE", [(date(2020, 1, 1), 0.01), (date(2026, 10, 30), 0.05)])
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T23:00:00Z"), req("m2", TS)], extra_lines=[
        web_search_use_line(TS, "tu1", "m1"), web_search_result_line(TS2, "tu1", 2),
        web_search_use_line(TS, "tu2", "m2"), web_search_result_line(TS2, "tu2", 2)])
    svc = service_for(root)
    assert svc.daily_spend(date(2026, 10, 29), date(2026, 10, 29))[0].cost == pytest.approx(0.02)
    assert total(svc) == pytest.approx(0.10)


def test_changing_the_rate_reprices_stored_requests_without_reading_transcripts(
        root, tmp_path, monkeypatch):
    root.write_transcript("p", "s1", [req("m1", TS)], extra_lines=_fee_lines())
    conn = store.connect(tmp_path / "s.sqlite")
    ingest.ingest(conn, [root.path])
    assert total(QueryService(conn, "UTC")) == pytest.approx(0.02)
    (root.path / "projects" / "p" / "s1.jsonl").unlink()  # the count is kept in the store
    monkeypatch.setattr(pricing, "WEB_SEARCH_FEE", [(date(2020, 1, 1), 0.02)])
    ingest.ingest(conn, [root.path])
    assert total(QueryService(conn, "UTC")) == pytest.approx(0.04)


def test_a_store_from_before_the_fee_gains_it_after_the_update(root, tmp_path):
    root.write_transcript("p", "s1", [req("m1", TS, output=1_000_000)], extra_lines=_fee_lines())
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)  # the store as the previous version left it: schema version 1
    old.row_factory = sqlite3.Row
    old.isolation_level = None
    old.execute("BEGIN")
    store.MIGRATIONS[0](old)
    old.execute("PRAGMA user_version = 1")
    old.execute("COMMIT")
    old.close()
    # the previous version ingested the transcripts: simulate its rows and markers
    conn = store.connect(path)
    ingest.ingest(conn, [root.path])
    with conn:
        conn.execute("DELETE FROM web_search_calls")
        conn.execute("UPDATE request_copies SET web_searches = 0")
        conn.execute("UPDATE requests SET web_searches = 0, cost_web_search = 0")
        conn.execute("DELETE FROM meta WHERE key = 'web_searches'")
        conn.execute("PRAGMA user_version = 1")
    conn.close()
    conn = store.connect(path)  # the update's migration step ...
    assert total(QueryService(conn, "UTC")) == pytest.approx(20.0)
    ingest.ingest(conn, [root.path])  # ... and the next ingest re-reads the transcripts
    assert total(QueryService(conn, "UTC")) == pytest.approx(20.02)


def test_an_explicit_search_count_of_zero_costs_nothing(root, service_for):
    svc = _one(root, service_for,
               web_search_use_line(TS, "tu1", "m1"), web_search_result_line(TS2, "tu1", 0))
    assert total(svc) == 0


@pytest.mark.parametrize("owner, other", [("s1", "s2"), ("s2", "s1")])
def test_a_result_held_only_by_another_transcript_is_charged_once(root, service_for, owner, other):
    root.write_transcript("p", owner, [req("m1", TS)],
                          extra_lines=[web_search_use_line(TS, "tu1", "m1")])
    root.write_transcript("p", other, [req("m1", TS, session_id=owner)],
                          extra_lines=[web_search_result_line(TS2, "tu1", 2)])
    svc = service_for(root)
    assert (total(svc), len(svc.session_summaries())) == (pytest.approx(0.02), 1)


@pytest.mark.parametrize("owner, other", [("s1", "s2"), ("s2", "s1")])
def test_a_call_held_only_by_a_non_owner_transcript_is_still_charged(root, service_for, owner, other):
    root.write_transcript("p", owner, [req("m1", TS)])
    root.write_transcript("p", other, [req("m1", TS, session_id=owner)], extra_lines=[
        web_search_use_line(TS, "tu1", "m1"), web_search_result_line(TS2, "tu1", 2)])
    assert total(service_for(root)) == pytest.approx(0.02)


def test_the_fee_survives_deleting_the_transcript_that_held_the_result(root, tmp_path):
    root.write_transcript("p", "s1", [req("m1", TS)],
                          extra_lines=[web_search_use_line(TS, "tu1", "m1")])
    gone = root.write_transcript("p", "s2", [req("m1", TS, session_id="s1")],
                                 extra_lines=[web_search_result_line(TS2, "tu1", 2)])
    conn = store.connect(tmp_path / "store.sqlite")
    ingest.ingest(conn, [root.path])
    assert total(QueryService(conn, "UTC")) == pytest.approx(0.02)
    gone.unlink()
    ingest.ingest(conn, [root.path])
    assert total(QueryService(conn, "UTC")) == pytest.approx(0.02)


def _other_result(tool_use_id: str, **top) -> str:
    """A successful tool result of some other tool (Read, Bash...), as the transcript stores it."""
    rec = {"type": "user", "uuid": f"r-{tool_use_id}", "timestamp": TS2,
           "message": {"role": "user", "content": [
               {"type": "tool_result", "tool_use_id": tool_use_id, "content": "file text"}]}, **top}
    return json.dumps(rec)


def _stored_search_rows(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM web_search_calls").fetchone()[0]


def test_results_of_other_tools_are_not_stored_and_cost_nothing(root, tmp_path):
    others = [tool_use_line(TS, f"r{i}", "Read", {"file_path": "f"}, "m1") for i in range(3)]
    others += [_other_result(f"r{i}", toolUseResult={"type": "text", "file": {}}) for i in range(3)]
    root.write_transcript("p", "s1", [req("m1", TS)], extra_lines=others)
    conn = store.connect(tmp_path / "s.sqlite")
    ingest.ingest(conn, [root.path])
    assert (total(QueryService(conn, "UTC")), _stored_search_rows(conn)) == (0, 0)


def test_a_search_result_seen_before_its_call_is_kept_and_charged(root, tmp_path):
    root.write_transcript("p", "a", [req("m1", TS)], extra_lines=[web_search_result_line(TS2, "tu1", 2)])
    root.write_transcript("p", "b", [req("m1", TS, session_id="a")],
                          extra_lines=[web_search_use_line(TS, "tu1", "m1")])
    conn = store.connect(tmp_path / "s.sqlite")
    ingest.ingest(conn, [root.path])
    assert total(QueryService(conn, "UTC")) == pytest.approx(0.02)


def test_a_non_search_result_in_a_later_ingest_is_not_stored(root, tmp_path):
    root.write_transcript("p", "s1", [req("m1", TS)],
                          extra_lines=[web_search_use_line(TS, "tu1", "m1")])
    conn = store.connect(tmp_path / "s.sqlite")
    ingest.ingest(conn, [root.path])
    root.append_to_transcript("p", "s1", [], raw=_other_result("rd1") + "\n")
    ingest.ingest(conn, [root.path])
    assert _stored_search_rows(conn) == 1


def test_the_search_lookup_columns_are_indexed_and_the_step_is_idempotent(tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    store._to_v3(conn)  # again over a store that already has it
    cols = {tuple(c[2] for c in conn.execute(f"PRAGMA index_info({i[1]})"))
            for i in conn.execute("PRAGMA index_list(web_search_calls)")}
    assert {("tool_use_id",), ("message_id",)} <= cols


def test_the_fee_goes_when_the_transcript_holding_the_call_is_rewritten_without_it(root, tmp_path):
    uses = [web_search_use_line(TS, "tu1", "m1"), web_search_result_line(TS2, "tu1", 2)]
    root.write_transcript("p", "s1", [req("m1", TS)], extra_lines=uses)
    root.write_transcript("p", "s2", [req("m1", TS, session_id="s1")])
    conn = store.connect(tmp_path / "s.sqlite")
    ingest.ingest(conn, [root.path])
    assert total(QueryService(conn, "UTC")) == pytest.approx(0.02)
    root.write_transcript("p", "s1", [req("m2", TS)])  # replaced by a shorter file, call gone
    ingest.ingest(conn, [root.path])
    assert total(QueryService(conn, "UTC")) == 0


def test_a_negative_search_count_costs_nothing(root, service_for):
    svc = _one(root, service_for, web_search_use_line(TS, "tu1", "m1"),
               web_search_result_line(TS2, "tu1", -3))
    assert total(svc) == 0
