"""Seam 1: Wide subagent and Fork subagent Opportunities through the query service."""
import io
from datetime import date

import pytest

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService

from conftest import NOW
from helpers import compact_line, req

# claude-opus-5-5: input $5/M? use cost relations rather than literals where possible


def _by_id(findings):
    return {f.id: f for f in findings}


def _sub(root, agent, reqs, fork=False, sid="s1"):
    meta = {"agentType": "fork", "isFork": True} if fork else {"agentType": "general-purpose"}
    root.write_subagent_transcript("p", sid, agent, reqs, meta=meta)


def _setup(root):
    root.write_transcript("p", "s1", [req("main", "2026-10-30T10:00:00Z", input=1_000)])
    _sub(root, "wide1", [req("w-a", "2026-10-30T10:01:00Z", input=60_000),
                         req("w-b", "2026-10-30T10:02:00Z", input=1_000)])
    _sub(root, "small1", [req("s-a", "2026-10-30T10:03:00Z", input=10_000)])
    _sub(root, "fork1", [req("f-a", "2026-10-30T10:04:00Z", input=2_000)], fork=True)


def test_wide_subagent_covers_subagents_with_large_first_request(root, service_for):
    _setup(root)
    f = _by_id(service_for(root).findings())["wide-subagent"]
    assert f.kind == "opportunity"
    assert f.avoidable == 0.0  # lower bound: delegating wide may be intentional
    assert {r.message_id for r in f.requests} == {"w-a", "w-b"}
    assert f.attributed == pytest.approx(sum(r.cost for r in f.requests))
    assert [(s.agent_id, s.requests) for s in f.subagents] == [("wide1", 2)]
    assert f.subagents[0].session_id == "s1"


def test_wide_threshold_is_configurable(root, tmp_path):
    _setup(root)
    conn = store.connect(tmp_path / "t.sqlite")
    ingest.ingest(conn, [root.path])
    svc = QueryService(conn, "UTC", thresholds={"wide_subagent_context_tokens": 5_000})
    f = _by_id(svc.findings())["wide-subagent"]
    assert {s.agent_id for s in f.subagents} == {"wide1", "small1"}


def test_fork_subagent_uses_fork_flag(root, service_for):
    _setup(root)
    f = _by_id(service_for(root).findings())["fork-subagent"]
    assert f.kind == "opportunity"
    assert f.formula is None and f.avoidable is None
    assert [r.message_id for r in f.requests] == ["f-a"]
    assert [s.agent_id for s in f.subagents] == ["fork1"]
    assert all(r.avoidable is None for r in f.requests)


def test_wide_subagent_whose_first_request_precedes_the_period(root, service_for):
    from datetime import date
    root.write_transcript("p", "s1", [req("main", "2026-10-30T10:00:00Z", input=1_000)])
    _sub(root, "wide1", [req("w-a", "2026-10-29T10:01:00Z", input=60_000),
                         req("w-b", "2026-10-30T10:02:00Z", input=1_000)])
    f = _by_id(service_for(root).findings(date(2026, 10, 30), date(2026, 10, 30)))["wide-subagent"]
    assert [r.message_id for r in f.requests] == ["w-b"]


# --- Wide subagent's Avoidable range (#107) ------------------------------------------------------
# claude-opus-5-5: input $4/M, cache read $0.20/M, 5m write $5/M. Wide subagent "wide1" (general-purpose,
# 60K first request): 55K of it is extra over the 5K fallback Bare subagent context.

EXTRA = 55_000
READ = 0.2 / 1e6
WRITE = 4 / 1e6  # the first request sent its context uncached: input rate
BARE = "bare_subagent_context_tokens"
HAIKU = "claude-haiku-5-5"


def _wide(root, reqs=None, agent="wide1", extra_lines=None, sid="s1", agent_type="general-purpose"):
    reqs = reqs or [req("w-a", "2026-10-30T10:01:00Z", input=60_000),
                    req("w-b", "2026-10-30T10:02:00Z", cache_read=60_000),
                    req("w-c", "2026-10-30T10:03:00Z", cache_read=60_000)]
    root.write_transcript("p", sid, [req("main", "2026-10-30T10:00:00Z", input=1_000)])
    root.write_subagent_transcript("p", sid, agent, reqs, meta={"agentType": agent_type},
                                   extra_lines=extra_lines)


def _runs(root, agent_type, n, context, fork=False):
    """`n` one-request subagents of a type, each starting at `context` tokens."""
    for i in range(n):
        root.write_subagent_transcript(
            "p", f"{agent_type}-s{i}", f"{agent_type}-{i}",
            [req(f"{agent_type}-{i}-a", "2026-10-20T10:00:00Z", input=context)],
            meta={"agentType": agent_type, "isFork": True} if fork else {"agentType": agent_type})


def _wide_finding(svc, *args):
    return _by_id(svc.findings(*args))["wide-subagent"]


def _ranged(f):
    return {r.message_id: (r.avoidable, r.avoidable_high) for r in f.requests}


def test_wide_subagent_avoidable_is_zero_to_the_write_and_reads_of_the_extra_context(root, service_for):
    _wide(root)
    f = _wide_finding(service_for(root))
    assert f.kind == "opportunity" and f.avoidable == 0.0
    assert f.avoidable_high == pytest.approx(EXTRA * (WRITE + 2 * READ))
    assert _ranged(f) == {"w-a": (0.0, pytest.approx(EXTRA * WRITE)),
                          "w-b": (0.0, pytest.approx(EXTRA * READ)),
                          "w-c": (0.0, pytest.approx(EXTRA * READ))}
    (s,) = f.sessions
    assert (s.avoidable, s.avoidable_high) == (0.0, pytest.approx(f.avoidable_high))
    assert sum(h for _, h in _ranged(f).values()) == pytest.approx(f.avoidable_high)


def test_without_a_qualifying_type_the_bare_context_falls_back_to_5000(root, service_for):
    _runs(root, "web-fetch", 19, 3_800)  # one run short of 20
    _wide(root)
    f = _wide_finding(service_for(root))
    assert "bare context 5.0K (fallback)" in f.formula
    assert f.avoidable_high == pytest.approx(EXTRA * (WRITE + 2 * READ))


def test_bare_context_is_the_median_of_the_leanest_type_with_20_non_fork_runs(root, service_for):
    _runs(root, "web-fetch", 20, 3_800)
    _runs(root, "Explore", 25, 45_000)   # qualifies, but not the leanest
    _runs(root, "tiny", 19, 100)         # leaner, but too few runs
    _runs(root, "fork", 30, 100, fork=True)  # forks never count
    _wide(root)
    f = _wide_finding(service_for(root))
    assert "bare context 3.8K (web-fetch median, 20 runs)" in f.formula
    assert f.avoidable_high == pytest.approx((60_000 - 3_800) * (WRITE + 2 * READ))


def test_the_config_override_wins_over_the_learned_context(root, service_for):
    _runs(root, "web-fetch", 20, 3_800)
    _wide(root)
    f = _wide_finding(service_for(root, thresholds={BARE: 20_000}))
    assert "bare context 20.0K (config)" in f.formula
    assert f.avoidable_high == pytest.approx(40_000 * (WRITE + 2 * READ))


def test_extra_context_is_never_below_zero(root, service_for):
    _wide(root)
    f = _wide_finding(service_for(root, thresholds={BARE: 80_000}))
    assert f.avoidable == 0.0
    assert _ranged(f) == {"w-a": (None, None), "w-b": (None, None), "w-c": (None, None)}  # nothing to price
    assert f.avoidable_high is None


def test_a_fork_stays_covered_without_an_avoidable_figure(root, service_for):
    _wide(root)
    root.write_subagent_transcript("p", "s1", "fork1", [req("f-a", "2026-10-30T10:04:00Z", input=60_000),
                                                       req("f-b", "2026-10-30T10:05:00Z", cache_read=60_000)],
                                   meta={"agentType": "fork", "isFork": True})
    f = _wide_finding(service_for(root))
    assert {r.message_id for r in f.requests} == {"w-a", "w-b", "w-c", "f-a", "f-b"}
    assert _ranged(f)["f-a"] == (None, None) and _ranged(f)["f-b"] == (None, None)
    assert f.avoidable_high == pytest.approx(EXTRA * (WRITE + 2 * READ))
    assert {s.agent_id for s in f.subagents} == {"wide1", "fork1"}


def test_a_subagent_whose_first_request_precedes_the_period_has_no_write(root, service_for):
    _wide(root, [req("w-a", "2026-10-29T10:01:00Z", input=60_000),
                 req("w-b", "2026-10-30T10:02:00Z", cache_read=60_000),
                 req("w-c", "2026-10-30T10:03:00Z", cache_read=60_000)])
    f = _wide_finding(service_for(root), date(2026, 10, 30), date(2026, 10, 30))
    assert f.avoidable_high == pytest.approx(2 * EXTRA * READ)
    assert _ranged(f) == {"w-b": (0.0, pytest.approx(EXTRA * READ)), "w-c": (0.0, pytest.approx(EXTRA * READ))}


def test_a_one_request_subagent_costs_its_write(root, service_for):
    _wide(root, [req("w-a", "2026-10-30T10:01:00Z", input=60_000)])
    f = _wide_finding(service_for(root))
    assert _ranged(f) == {"w-a": (0.0, pytest.approx(EXTRA * WRITE))}


def test_the_writes_cached_part_is_charged_at_the_write_rate_and_the_rest_at_input(root, service_for):
    _wide(root, [req("w-a", "2026-10-30T10:01:00Z", input=10_000, write_5m=50_000)])
    f = _wide_finding(service_for(root))
    assert f.avoidable_high == pytest.approx(50_000 * 5 / 1e6 + 5_000 * WRITE)


def test_a_cold_write_in_the_stream_swaps_its_read_for_the_write(root, service_for):
    _wide(root, [req("w-a", "2026-10-30T10:01:00Z", input=60_000),
                 req("w-b", "2026-10-30T10:02:00Z", cache_read=60_000),
                 req("w-c", "2026-10-30T10:30:00Z", write_5m=60_000)])  # idle past the 5m TTL: Cold
    f = _wide_finding(service_for(root))
    cold = EXTRA * (5 / 1e6 - READ)
    assert _ranged(f)["w-c"] == (0.0, pytest.approx(EXTRA * READ + cold))
    assert f.avoidable_high == pytest.approx(EXTRA * (WRITE + 2 * READ) + cold)


def test_a_prompt_tier_drop_raises_the_upper_bound(root, service_for):
    """Haiku 5.5: x5 above 100K. Without the 85K extra both prompts (105K) fall to 20K."""
    _wide(root, [req("w-a", "2026-10-30T10:01:00Z", model=HAIKU, input=105_000),
                 req("w-b", "2026-10-30T10:02:00Z", model=HAIKU, cache_read=105_000)])
    f = _wide_finding(service_for(root, thresholds={BARE: 20_000}))
    a_cost, b_cost = 105_000 * 0.5 / 1e6, 105_000 * 0.05 / 1e6  # tiered input / read
    assert _ranged(f)["w-a"] == (0.0, pytest.approx(85_000 * 0.5 / 1e6 + a_cost * 4 / 5))
    assert _ranged(f)["w-b"] == (0.0, pytest.approx(85_000 * 0.05 / 1e6 + b_cost * 4 / 5))


def test_the_block_ends_at_the_streams_first_compaction(root, service_for):
    _wide(root, extra_lines=[compact_line("c1", "2026-10-30T10:02:30Z", 60_000, 5_000)])
    f = _wide_finding(service_for(root))
    assert _ranged(f) == {"w-a": (0.0, pytest.approx(EXTRA * WRITE)),
                          "w-b": (0.0, pytest.approx(EXTRA * READ)),
                          "w-c": (0.0, 0.0)}


def test_a_compaction_ends_only_the_block_of_the_session_it_happened_in(root, service_for):
    """The same agent id in two Sessions: one Session's compaction does not end the other's block."""
    for sid, day in (("s1", "10"), ("s2", "11")):
        root.write_transcript("p", sid, [req(f"{sid}-main", f"2026-10-{day}T09:00:00Z", input=1_000)])
        root.write_subagent_transcript(
            "p", sid, "wide1",
            [req(f"{sid}-a", f"2026-10-{day}T10:01:00Z", input=60_000),
             req(f"{sid}-b", f"2026-10-{day}T10:02:00Z", cache_read=60_000),
             req(f"{sid}-c", f"2026-10-{day}T10:03:00Z", cache_read=60_000)],
            meta={"agentType": "general-purpose"},
            extra_lines=[compact_line("c1", "2026-10-10T10:02:30Z", 60_000, 5_000)] if sid == "s1" else None)
    f = _wide_finding(service_for(root))
    assert _ranged(f) == {"s1-a": (0.0, pytest.approx(EXTRA * WRITE)),
                          "s1-b": (0.0, pytest.approx(EXTRA * READ)),
                          "s1-c": (0.0, 0.0),
                          "s2-a": (0.0, pytest.approx(EXTRA * WRITE)),
                          "s2-b": (0.0, pytest.approx(EXTRA * READ)),
                          "s2-c": (0.0, pytest.approx(EXTRA * READ))}


def test_the_per_request_figures_sum_to_the_upper_bound_with_a_cold_write_and_a_compaction(root, service_for):
    _wide(root, [req("w-a", "2026-10-30T10:01:00Z", input=60_000),
                 req("w-b", "2026-10-30T10:02:00Z", cache_read=60_000),
                 req("w-c", "2026-10-30T10:30:00Z", write_5m=60_000),  # Cold
                 req("w-d", "2026-10-30T10:31:00Z", cache_read=60_000)],
          extra_lines=[compact_line("c1", "2026-10-30T10:30:30Z", 60_000, 5_000)])
    f = _wide_finding(service_for(root))
    assert f.avoidable_high > 0
    assert sum(h for _, h in _ranged(f).values()) == pytest.approx(f.avoidable_high)


def test_a_compaction_before_the_period_leaves_nothing_to_charge(root, service_for):
    _wide(root, [req("w-a", "2026-10-29T10:01:00Z", input=60_000),
                 req("w-b", "2026-10-30T10:02:00Z", cache_read=60_000)],
          extra_lines=[compact_line("c1", "2026-10-29T12:00:00Z", 60_000, 5_000)])
    f = _wide_finding(service_for(root), date(2026, 10, 30), date(2026, 10, 30))
    assert _ranged(f) == {"w-b": (None, None)}  # the block ended before the Period: no figure


def test_a_zero_config_value_is_a_real_bare_context_not_learn_it(root, service_for):
    _runs(root, "web-fetch", 20, 3_800)
    _wide(root)
    f = _wide_finding(service_for(root, thresholds={BARE: 0}))
    assert "bare context 0.0K (config)" in f.formula
    assert f.avoidable_high == pytest.approx(60_000 * (WRITE + 2 * READ))


def test_an_unset_bare_context_is_listed_as_learned_and_zero_can_be_set(root, configure):
    env = configure(root)
    out = io.StringIO()
    cli.main(["config"], env=env, now=NOW, out=out)
    assert f"{BARE} = (learned)" in out.getvalue()
    cli.main(["config", "set", BARE, "0"], env=env, now=NOW, out=io.StringIO())
    out = io.StringIO()
    cli.main(["config"], env=env, now=NOW, out=out)
    assert f"{BARE} = 0 " in out.getvalue()


def test_repeated_agent_ids_across_sessions_keep_their_own_type_and_fork_flag(root, service_for):
    """A forked run reusing a leaner run's agent id in another Session does not take it out of its type."""
    _runs(root, "web-fetch", 20, 3_800)
    root.write_subagent_transcript("p", "other-s", "web-fetch-0",
                                   [req("dup-a", "2026-10-21T10:00:00Z", input=100)],
                                   meta={"agentType": "fork", "isFork": True})
    _wide(root)
    f = _wide_finding(service_for(root))
    assert "bare context 3.8K (web-fetch median, 20 runs)" in f.formula


def test_a_replayed_compaction_uuid_ends_the_block_in_each_session(root, service_for):
    """A resumed Session replays its Subagent transcripts, compact_boundary uuids included."""
    for sid, day in (("s1", "10"), ("s2", "11")):
        root.write_transcript("p", sid, [req(f"{sid}-main", f"2026-10-{day}T09:00:00Z", input=1_000)])
        root.write_subagent_transcript(
            "p", sid, "wide1",
            [req(f"{sid}-a", f"2026-10-{day}T10:01:00Z", input=60_000),
             req(f"{sid}-b", f"2026-10-{day}T10:03:00Z", cache_read=60_000)],
            meta={"agentType": "general-purpose"},
            extra_lines=[compact_line("c1", f"2026-10-{day}T10:02:00Z", 60_000, 5_000)])
    f = _wide_finding(service_for(root))
    assert _ranged(f) == {"s1-a": (0.0, pytest.approx(EXTRA * WRITE)), "s1-b": (0.0, 0.0),
                          "s2-a": (0.0, pytest.approx(EXTRA * WRITE)), "s2-b": (0.0, 0.0)}


def test_the_bare_context_is_loaded_only_when_a_wide_subagent_needs_it(root, service_for, monkeypatch):
    from claude_cost import cache
    calls = []
    real = cache.bare_subagent_context
    monkeypatch.setattr(cache, "bare_subagent_context", lambda conn: (calls.append(1), real(conn))[1])
    root.write_transcript("p", "s1", [req("main", "2026-10-30T10:00:00Z", input=1_000)])
    service_for(root).findings()
    assert calls == []
    _wide(root)
    service_for(root).findings()
    assert calls == [1]


def test_drains_command_and_pages_show_the_wide_subagent_range(root, configure):
    _wide(root)
    out = io.StringIO()
    cli.main(["drains"], env=configure(root), now=NOW, out=out)
    line = next(l for l in out.getvalue().splitlines() if "Wide subagent" in l)
    assert "avoidable $0.00–$0.24" in line
    assert "bare context 5.0K (fallback)" in out.getvalue()
    from claude_cost.query import QueryService
    from claude_cost.web import create_app
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone, thresholds=cfg.thresholds)
    client = create_app(factory, lambda: NOW).test_client()
    for url in ("/findings", "/findings/wide-subagent", "/"):
        assert "$0.00–$0.24" in client.get(url).get_data(as_text=True), url
    page = client.get("/findings/wide-subagent").get_data(as_text=True)
    assert "bare context 5.0K (fallback)" in page and "$0.00–$0.22" in page  # per-request range


def test_config_lists_the_bare_subagent_context_override(root, configure):
    out = io.StringIO()
    cli.main(["config"], env=configure(root), now=NOW, out=out)
    assert BARE in out.getvalue()
