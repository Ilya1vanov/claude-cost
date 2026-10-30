"""Seam 1: ingest a synthetic Data root, assert daily totals through the query service."""
from datetime import date

import pytest

from helpers import req

D29, D30 = date(2026, 10, 29), date(2026, 10, 30)


def costs(service, start, end):
    return {d.day: d.cost for d in service.daily_spend(start, end)}


def test_spend_per_day_priced_from_the_table(root, service_for):
    # opus-5-5: $4 in, $20 out, $0.20 read, writes 1.25x / 2x of input
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-29T10:00:00Z", input=1000, output=2000, cache_read=10000,
            write_5m=4000, write_1h=1000),
        req("m2", "2026-10-30T09:00:00Z", output=1_000_000),
    ])
    c = costs(service_for(root), D29, D30)
    assert c[D29] == pytest.approx(0.074)
    assert c[D30] == pytest.approx(20.0)


def test_current_models_have_prices(root, service_for):
    kw = dict(input=1000, output=2000, cache_read=10000, write_5m=4000, write_1h=1000)
    root.write_transcript("p", "s1", [
        req("sonnet", "2026-10-30T01:00:00Z", model="claude-sonnet-5-5", **kw),  # read $0.10
    ])
    root.write_transcript("p", "s2", [
        req("fable", "2026-10-30T02:00:00Z", model="claude-fable-5-1", **kw),
        req("haiku", "2026-10-30T03:00:00Z", model="claude-haiku-4-5-20251001", **kw),
    ])
    svc = service_for(root)
    # sonnet 0.037; fable 0.01+0.1+0.0025+0.05+0.02 = 0.1825; haiku 0.001+0.01+0.001+0.005+0.002 = 0.019
    assert costs(svc, D30, D30)[D30] == pytest.approx(0.037 + 0.1825 + 0.019)
    assert svc.unpriced_models() == []


def test_unknown_model_is_flagged_not_priced(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", model="claude-future-9", output=1000)])
    svc = service_for(root)
    assert svc.unpriced_models() == ["claude-future-9"]
    assert costs(svc, D30, D30)[D30] == 0.0


def test_streamed_lines_count_once_with_final_output(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000, lines=3)])
    svc = service_for(root)
    day = svc.daily_spend(D30, D30)[0]
    assert (day.requests, day.cost) == (1, pytest.approx(20.0))


def test_request_copied_into_another_transcript_counts_once_for_its_owner(root, service_for):
    r = req("m1", "2026-10-30T01:00:00Z", output=1_000_000, session_id="orig")
    root.write_transcript("p", "orig", [r])
    root.write_transcript("p", "resumed", [r])  # copy keeps sessionId=orig
    day = service_for(root).daily_spend(D30, D30)[0]
    assert (day.requests, day.cost) == (1, pytest.approx(20.0))


def test_skips_synthetic_corrupt_lines_and_superseded_copies_but_counts_subagents(root, service_for):
    f = root.write_transcript("p", "s1", [
        req("m1", "2026-10-30T01:00:00Z", output=1_000_000),
        req("syn", "2026-10-30T02:00:00Z", model="<synthetic>", output=999),
    ], extra_lines=['{"type": "assistant", "broken'])
    root.write_subagent_transcript("p", "s1", "a1", [req("sub", "2026-10-30T03:00:00Z", output=1_000_000)])
    (f.parent / "s1.jsonl.superseded-1760000000000").write_text(f.read_text())
    day = service_for(root).daily_spend(D30, D30)[0]
    assert (day.requests, day.cost) == (2, pytest.approx(40.0))  # m1 + the subagent's request


def test_days_follow_the_configured_timezone_and_gaps_are_zero(root, tmp_path):
    from claude_cost import ingest, store
    from claude_cost.query import QueryService
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T23:30:00Z", output=1_000_000)])
    conn = store.connect(tmp_path / "tz.sqlite")
    ingest.ingest(conn, [root.path])
    svc = QueryService(conn, "Europe/Kyiv")  # UTC+3 in October: lands on the 30th
    c = costs(svc, date(2026, 10, 28), D30)
    assert c == {date(2026, 10, 28): 0.0, D29: 0.0, D30: pytest.approx(20.0)}


def test_ingest_twice_does_not_double(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000)])
    svc = service_for(root)
    svc2 = service_for(root)  # same store path, second ingest
    assert svc2.daily_spend(D30, D30)[0].cost == pytest.approx(20.0)
