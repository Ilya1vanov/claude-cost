"""Seam 1: Compact threshold, Oversized context Drain and Late compacts through the query service."""
import pytest

from claude_cost.compact import WindowClamp
from helpers import compact_line, req

# claude-opus-5-5: cache read $0.20/M, output $20/M, 1h write $8/M.
# A compaction to 10K tokens costs 10K x (20 + 8) / 1M = $0.28. With context growing 5K per
# request, amortised cost per request at threshold T is 0.1e-6 x (T + post) + 0.28 x 5K / (T - post),
# minimised at T - post = sqrt(0.28 x 5000 / 0.1e-6) = ~118K, so T = ~128K (grid of 10K: 130K).


def _ramp_sessions(root, n, model="claude-opus-5-5", post=10_000, project="p", prefix="s"):
    """n sessions whose context grows 5K per request up to 500K, then compacts to `post`."""
    for i in range(n):
        reqs = [req(f"{prefix}{i}-{k}", f"2026-10-0{1 + i % 9}T{k // 60:02d}:{k % 60:02d}:00Z",
                    model=model, cache_read=5_000 * (k + 1)) for k in range(100)]
        root.write_transcript(project, f"{prefix}{i}", reqs, extra_lines=[
            compact_line(f"c-{prefix}{i}", f"2026-10-0{1 + i % 9}T05:00:00Z", 500_000, post, "auto")])


def test_threshold_is_the_minimum_of_the_cost_curve(root, service_for):
    _ramp_sessions(root, 20)
    a = service_for(root).compact_thresholds()
    [m] = a.models
    assert (m.model, m.source, m.sample_size, m.post_tokens_p75) == ("claude-opus-5-5", "model", 20, 10_000)
    assert m.threshold == 130_000
    assert m.compaction_cost == pytest.approx(0.28)
    by_t = {c.threshold: c.cost for c in m.curve}
    assert by_t[130_000] < by_t[100_000] and by_t[130_000] < by_t[200_000]  # a clear interior optimum
    assert m.payback_requests == pytest.approx(0.28 / (120_000 * 0.2e-6))  # ~117 requests
    assert m.actual_cost > by_t[130_000]


def test_compaction_cost_uses_p75_of_observed_compactions(root, service_for):
    _ramp_sessions(root, 15, post=10_000, prefix="a")
    _ramp_sessions(root, 5, post=40_000, prefix="b")
    [m] = service_for(root).compact_thresholds().models
    assert m.post_tokens_p75 == 10_000  # 15 of 20 are 10K: p75 is the 15th
    _ramp_sessions(root, 6, post=40_000, prefix="c")
    [m] = service_for(root).compact_thresholds().models
    assert m.post_tokens_p75 == 40_000  # 15 of 26 are 10K: p75 is the 20th


def test_model_with_few_compactions_falls_back_to_global(root, service_for):
    _ramp_sessions(root, 20)                                   # opus: own threshold
    _ramp_sessions(root, 3, model="claude-sonnet-5-5", prefix="x")
    a = service_for(root).compact_thresholds()
    opus = a.for_model("claude-opus-5-5")
    sonnet = a.for_model("claude-sonnet-5-5")
    assert (opus.source, sonnet.source) == ("model", "global")
    assert sonnet.sample_size == 3
    assert sonnet.threshold == a.global_.threshold
    assert a.for_model("claude-haiku-5-5") is a.global_  # no compactions at all


def test_no_compactions_means_no_threshold_and_no_oversized_drain(root, service_for):
    root.write_transcript("p", "s", [req("m1", "2026-10-30T10:00:00Z", cache_read=900_000)])
    svc = service_for(root)
    assert svc.compact_thresholds().global_ is None
    f = {f.id: f for f in svc.findings()}["oversized-context"]
    assert f.requests == () and f.avoidable == 0.0


def test_oversized_context_drain_is_reads_above_threshold_minus_one_compaction(root, service_for):
    _ramp_sessions(root, 20)
    root.write_transcript("p", "big", [req(f"b{k}", f"2026-10-20T10:0{k}:00Z", cache_read=c)
                                       for k, c in enumerate([100_000, 150_000, 600_000, 900_000, 1_200_000])])
    f = {f.id: f for f in service_for(root).findings()}["oversized-context"]
    big = [r for r in f.requests if r.session_id == "big"]
    assert [r.message_id for r in big] == ["b1", "b2", "b3", "b4"]  # above T=130K
    assert [round(r.avoidable, 6) for r in big] == [
        round(20_000 * 0.2e-6 - 0.28, 6),  # first one also pays for the compaction
        round(470_000 * 0.2e-6, 6), round(770_000 * 0.2e-6, 6), round(1_070_000 * 0.2e-6, 6)]
    [s] = [s for s in f.sessions if s.session_id == "big"]
    assert s.avoidable == pytest.approx(0.004 - 0.28 + 0.094 + 0.154 + 0.214)
    assert f.kind == "drain" and f.formula


def _ramp_to(root, ctxs, post, model="claude-opus-5-5"):
    reqs = [req(f"r{k}", f"2026-10-01T10:{k:02d}:00Z", model=model, cache_read=c) for k, c in enumerate(ctxs)]
    root.write_transcript("p", "r", reqs, extra_lines=[
        compact_line("c", "2026-10-01T11:00:00Z", ctxs[-1], post, "auto")])


def test_autocompact_window_is_threshold_plus_the_autocompact_offset(root, service_for):
    _ramp_sessions(root, 20)                                   # opus: own threshold 130K
    _ramp_sessions(root, 3, model="claude-sonnet-5-5", prefix="x")
    a = service_for(root).compact_thresholds()
    opus, sonnet = a.for_model("claude-opus-5-5"), a.for_model("claude-sonnet-5-5")
    assert (opus.autocompact_window, opus.window_clamp) == (163_000, None)
    assert (sonnet.source, sonnet.autocompact_window) == ("global", a.global_.threshold + 33_000)
    assert a.global_.autocompact_window == a.global_.threshold + 33_000


def test_autocompact_window_is_raised_to_the_minimum_claude_code_accepts(root, service_for):
    _ramp_to(root, [5_000 * (k + 1) for k in range(10)], 10_000)
    low = service_for(root).compact_thresholds().global_
    assert low.threshold + 33_000 < 100_000
    assert (low.autocompact_window, low.window_clamp) == (100_000, WindowClamp.RAISED)


def test_autocompact_window_is_lowered_to_the_maximum_claude_code_accepts(root, service_for):
    _ramp_to(root, [100_000 * (k + 1) for k in range(12)], 990_000)
    high = service_for(root).compact_thresholds().global_
    assert high.threshold + 33_000 > 1_000_000
    assert (high.autocompact_window, high.window_clamp) == (1_000_000, WindowClamp.LOWERED)


def test_timeline_shows_threshold_and_highlights_late_compacts(root, service_for):
    _ramp_sessions(root, 20)
    root.write_transcript("p", "t", [req("t1", "2026-10-25T10:00:00Z", cache_read=30_000),
                                     req("t2", "2026-10-25T10:01:00Z", cache_read=45_000),
                                     req("t3", "2026-10-25T10:02:00Z", cache_read=45_000),
                                     req("t4", "2026-10-25T10:04:00Z", cache_read=150_000)],
                          extra_lines=[compact_line("tc1", "2026-10-25T10:01:30Z", 45_000, 8_000, "manual"),
                                       compact_line("tc2", "2026-10-25T10:03:00Z", 200_000, 8_000, "auto"),
                                       compact_line("tc3", "2026-10-25T10:05:00Z", 20_000, 8_000, "manual")])
    t = service_for(root).session_timeline("t")
    assert all(p.threshold == 130_000 for p in t.points)
    early, high_pretokens_low_request, late = t.compactions
    # Judged by the request before it, not by preTokens: 45K <= T < 150K; 200K preTokens don't matter
    assert (early.late, high_pretokens_low_request.late, late.late) == (False, False, True)
    assert early.threshold == 130_000


def test_compaction_with_no_request_before_it_is_not_late(root, service_for):
    _ramp_sessions(root, 20)
    root.write_transcript("p", "e", [req("e1", "2026-10-25T10:05:00Z", cache_read=150_000)],
                          extra_lines=[compact_line("ec", "2026-10-25T10:00:00Z", 200_000, 8_000, "manual")])
    (c,) = service_for(root).session_timeline("e").compactions
    assert (c.threshold, c.late) == (None, False)


def test_compaction_after_a_request_whose_model_has_no_threshold_is_not_late(root, service_for):
    # An unpriced model is not replayed, so no compaction counts and there is no global threshold
    root.write_transcript("p", "u", [req("u1", "2026-10-25T10:00:00Z", model="mystery-model",
                                         cache_read=500_000)],
                          extra_lines=[compact_line("uc", "2026-10-25T10:03:00Z", 500_000, 8_000, "auto")])
    svc = service_for(root)
    assert svc.compact_thresholds().global_ is None
    (c,) = svc.session_timeline("u").compactions
    assert (c.threshold, c.late) == (None, False)


def test_timeline_without_compaction_history_has_no_threshold(root, service_for):
    root.write_transcript("p", "t", [req("t1", "2026-10-25T10:00:00Z", cache_read=30_000)])
    t = service_for(root).session_timeline("t")
    assert t.points[0].threshold is None


def test_cli_compact_and_pages(root, configure):
    import html
    import io
    import json
    import re
    from claude_cost import cli, ingest, store
    from claude_cost.query import QueryService
    from claude_cost.web import create_app
    from conftest import NOW

    _ramp_sessions(root, 20)
    root.write_transcript("p", "t", [req("t1", "2026-10-25T10:00:00Z", cache_read=150_000)],
                          extra_lines=[compact_line("tc", "2026-10-25T10:03:00Z", 20_000, 8_000, "auto")])
    root.write_transcript("p", "n", [req("n1", "2026-10-25T11:00:00Z", cache_read=30_000)],
                          extra_lines=[compact_line("nc", "2026-10-25T11:03:00Z", 200_000, 8_000, "auto")])
    env = configure(root)
    out = io.StringIO()
    assert cli.main(["compact"], env=env, now=NOW, out=out) == 0
    text = out.getvalue()
    assert "claude-opus-5-5: Compact threshold 130,000 tokens (model)" in text and "payback" in text
    assert "autoCompactWindow 163,000" in text
    snippet = json.loads(text[text.index("{\n"):])
    assert snippet == {"modelSettings": {"claude-opus-5-5": {"autoCompactWindow": 163_000}}}
    cfg = cli.load_config(env)

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    client = create_app(factory, lambda: NOW).test_client()
    page = client.get("/compact").get_data(as_text=True)
    assert "130,000" in page and "Cost vs threshold" in page
    assert "Recommended autoCompactWindow" in page and "163,000" in page
    assert "33,000" in page and "2.1.314" in page
    page_snippet = json.loads(html.unescape(re.search(r'<pre id="model-settings">(.*?)</pre>', page, re.S).group(1)))
    assert page_snippet == snippet
    assert "Late compact" in client.get("/session/t").get_data(as_text=True)
    assert "Late compact" not in client.get("/session/n").get_data(as_text=True)  # high preTokens, low request


def test_cli_and_page_show_the_clamp_note_and_global_badge(root, configure):
    import io
    from claude_cost import cli, ingest, store
    from claude_cost.query import QueryService
    from claude_cost.web import create_app
    from conftest import NOW

    # One compaction: opus falls back to the global threshold, and threshold + 33K < 100K
    _ramp_to(root, [5_000 * (k + 1) for k in range(10)], 10_000)
    env = configure(root)
    out = io.StringIO()
    assert cli.main(["compact"], env=env, now=NOW, out=out) == 0
    assert "(raised to 100,000: autocompact fires at 67,000)" in out.getvalue()

    cfg = cli.load_config(env)

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    page = create_app(factory, lambda: NOW).test_client().get("/compact").get_data(as_text=True)
    assert "raised to 100,000: autocompact fires at 67,000" in page
    assert '<span class="badge">global</span>' in page


def test_min_sample_and_step_are_configurable(root, service_for):
    _ramp_sessions(root, 20)
    _ramp_sessions(root, 3, model="claude-sonnet-5-5", prefix="x")
    a = service_for(root, thresholds={"compact_min_sample": 3, "compact_step": 5_000}).compact_thresholds()
    assert a.for_model("claude-sonnet-5-5").source == "model"   # 3 compactions are now enough
    opus = a.for_model("claude-opus-5-5")
    steps = {b.threshold - a_.threshold for a_, b in zip(opus.curve, opus.curve[1:])}
    assert steps == {5_000}


def test_replay_resets_at_a_real_compaction_and_never_goes_negative():
    from datetime import date  # noqa: F401
    from claude_cost.compact import _Req, replay
    # contexts 100K, 150K, real compaction to 20K, 30K; T=120K, post=10K; read $1/token, post $0
    ctxs = [100_000, 150_000, 20_000, 30_000]
    reqs = [_Req(f"m{i}", ("s", None), float(i), "m", c, 1.0, 0.0) for i, c in enumerate(ctxs)]
    # the 150K request is hypothetically compacted to 10K; the real drop to 20K resets that
    assert replay(reqs, 120_000, 10_000) == 100_000 + 10_000 + 20_000 + 30_000


def test_payback_is_for_the_threshold_applied_and_the_models_own_prices(root, service_for):
    from claude_cost import pricing
    _ramp_sessions(root, 20)                                   # opus: own threshold
    _ramp_sessions(root, 3, model="claude-sonnet-5-5", prefix="x")
    root.write_transcript("p", "h", [req("h1", "2026-10-02T10:00:00Z", model="claude-haiku-5-5", cache_read=50_000)])
    a = service_for(root).compact_thresholds()
    table = pricing.build_table(None)

    def per_post(model):
        p = pricing.find_price(model, __import__("datetime").date(2026, 10, 2), table)
        return (p.output + p.input * pricing.WRITE_1H_MULT) / 1_000_000

    def read(model):
        return pricing.find_price(model, __import__("datetime").date(2026, 10, 2), table).cache_read / 1_000_000

    sonnet = a.for_model("claude-sonnet-5-5")
    assert sonnet.source == "global" and sonnet.threshold == a.global_.threshold
    assert sonnet.compaction_cost == pytest.approx(10_000 * per_post("claude-sonnet-5-5"))
    assert sonnet.payback_requests == pytest.approx(
        sonnet.compaction_cost / ((a.global_.threshold - 10_000) * read("claude-sonnet-5-5")))
    haiku = a.for_model("claude-haiku-5-5")  # no compactions: global T and p75, haiku prices
    assert haiku.threshold == a.global_.threshold
    assert haiku.compaction_cost == pytest.approx(a.global_.post_tokens_p75 * per_post("claude-haiku-5-5"))
    assert haiku.compaction_cost != pytest.approx(a.global_.compaction_cost)
