"""Seam 1: cache classification (Cold/Warm) and cache metrics through the query service."""
from datetime import date

import pytest

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req

# fixture model claude-opus-5-5: input $4/M, cache read $0.20/M, write 5m $5/M, write 1h $8/M


def _events(svc):
    return svc.cache_events()


def test_cold_resume_vs_warm_growth(root, service_for):
    root.write_transcript("p", "s1", [
        req("first", "2026-10-30T10:00:00Z", write_1h=100_000),            # new session: Warm
        req("grow", "2026-10-30T10:02:00Z", cache_read=100_000, write_1h=2_000),  # growth: Warm
        req("resume", "2026-10-30T13:00:00Z", write_1h=102_000),           # 3h idle > 1h TTL: Cold
        req("after", "2026-10-30T13:01:00Z", cache_read=102_000, write_1h=500),  # Warm again
    ])
    ev = _events(service_for(root))
    assert ev == {"first": "warm", "grow": "warm", "resume": "cold", "after": "warm"}


def test_ttl_follows_previous_write_and_idle_gap_inside_ttl_is_warm(root, service_for):
    root.write_transcript("p", "s1", [
        req("a", "2026-10-30T10:00:00Z", write_5m=1_000),
        req("b", "2026-10-30T10:10:00Z", write_5m=1_000),   # 10 min > 5m TTL: Cold
        req("c", "2026-10-30T10:50:00Z", write_1h=1_000),   # 40 min > 5m TTL: Cold
        req("d", "2026-10-30T11:30:00Z", write_1h=1_000),   # 40 min < 1h TTL: Warm
    ])
    assert _events(service_for(root)) == {"a": "warm", "b": "cold", "c": "cold", "d": "warm"}


def test_requests_without_a_write_have_no_write_event(root, service_for):
    root.write_transcript("p", "s1", [
        req("a", "2026-10-30T10:00:00Z", write_1h=1_000),
        req("b", "2026-10-30T10:01:00Z", cache_read=1_000, output=10),
    ])
    assert _events(service_for(root)) == {"a": "warm", "b": "none"}


def test_resume_across_transcripts_is_classified_in_the_logical_session(root, service_for):
    root.write_transcript("p", "s1", [req("a", "2026-10-30T10:00:00Z", write_1h=50_000)])
    # a resumed transcript: lines carry the old sessionId, so both are one Session
    root.write_transcript("p", "s2", [
        req("b", "2026-10-30T15:00:00Z", write_1h=50_000, session_id="s1")])
    assert _events(service_for(root)) == {"a": "warm", "b": "cold"}


def test_hit_ratio_cold_warm_and_ttl_split(root, service_for):
    root.write_transcript("p", "s1", [
        req("a", "2026-10-29T10:00:00Z", input=1_000, write_1h=100_000),
        req("b", "2026-10-29T10:01:00Z", input=1_000, cache_read=100_000, write_1h=1_000),
        req("c", "2026-10-29T14:00:00Z", write_1h=101_000),  # cold: $0.808
    ])
    card = service_for(root).cache_card("last7", NOW)
    cur = card.current
    # read 100K / (read 100K + write 202K + uncached 2K)
    assert cur.hit_ratio == pytest.approx(100_000 / 304_000)
    assert cur.cold_cost == pytest.approx(101_000 * 8 / 1e6)
    assert cur.warm_cost == pytest.approx(101_000 * 8 / 1e6)
    assert cur.write_1h_cost == pytest.approx(202_000 * 8 / 1e6)
    assert cur.write_5m_cost == 0
    assert cur.rebuilds == 1


def test_hit_ratio_is_none_without_tokens(root, service_for):
    root.write_transcript("p", "s1", [req("a", "2026-10-29T10:00:00Z", output=10)])
    assert service_for(root).cache_card("last7", NOW).current.hit_ratio is None


def test_card_has_baseline_and_deltas(root, service_for):
    root.write_transcript("p", "s1", [
        req("a", "2026-10-20T10:00:00Z", write_5m=100_000),                       # baseline
        req("b", "2026-10-20T12:00:00Z", write_5m=100_000),                       # cold
    ])
    root.write_transcript("p", "s2", [
        req("c", "2026-10-29T10:00:00Z", cache_read=300_000, write_1h=100_000),   # current
    ])
    card = service_for(root).cache_card("last7", NOW)
    assert card.baseline.rebuilds == 1 and card.current.rebuilds == 0
    assert card.baseline.hit_ratio == 0.0
    assert card.current.hit_ratio == pytest.approx(0.75)
    assert card.delta_hit_ratio == pytest.approx(0.75)
    assert card.baseline.cold_cost == pytest.approx(100_000 * 5 / 1e6)


# --- Cache loss: a Cold write's cost beyond reading the same tokens ----------------------------

def _loss(root, service_for, *reqs):
    root.write_transcript("p", "s1", list(reqs))
    return service_for(root).cache_card("last7", NOW)


def test_cold_write_loses_its_cost_beyond_a_read_of_the_same_tokens(root, service_for):
    card = _loss(root, service_for,
                 req("a", "2026-10-29T10:00:00Z", write_1h=100_000),   # warm
                 req("b", "2026-10-29T14:00:00Z", write_1h=100_000))   # cold: 1h write $8/M, read $0.2/M
    assert card.current.cache_loss == pytest.approx(100_000 * (8 - 0.2) / 1e6)


@pytest.mark.parametrize("ttl, write_rate", [("write_5m", 5), ("write_1h", 8)])
def test_5m_and_1h_cold_writes_of_equal_size_lose_different_amounts(root, service_for, ttl, write_rate):
    card = _loss(root, service_for,
                 req("a", "2026-10-29T10:00:00Z", **{ttl: 100_000}),
                 req("b", "2026-10-29T12:00:00Z", **{ttl: 100_000}))
    assert card.current.cache_loss == pytest.approx(100_000 * (write_rate - 0.2) / 1e6)


def test_a_cold_write_above_the_prompt_tier_uses_the_tiered_read_rate(root, service_for):
    """claude-haiku-5-5: 1h write $0.2/M, read $0.01/M; both x5 above a 100K prompt."""
    haiku = "claude-haiku-5-5"
    card = _loss(root, service_for,
                 req("a", "2026-10-29T10:00:00Z", model=haiku, write_1h=1_000),
                 req("b", "2026-10-29T14:00:00Z", model=haiku, write_1h=150_000),   # tiered
                 req("c", "2026-10-29T18:00:00Z", model=haiku, write_1h=50_000))    # flat
    assert card.current.cache_loss == pytest.approx(
        150_000 * (1.0 - 0.05) / 1e6 + 50_000 * (0.2 - 0.01) / 1e6)


def test_warm_writes_reads_and_uncached_input_are_not_cache_loss(root, service_for):
    card = _loss(root, service_for,
                 req("a", "2026-10-29T10:00:00Z", input=5_000, write_1h=100_000),
                 req("b", "2026-10-29T10:01:00Z", input=5_000, cache_read=100_000, write_1h=2_000))
    assert card.current.cache_loss == 0
    assert card.current.cache_loss_share == 0


def test_cache_loss_share_is_of_the_periods_total_cost_including_output(root, service_for):
    card = _loss(root, service_for,
                 req("a", "2026-10-29T10:00:00Z", write_1h=100_000),                  # $0.80
                 req("b", "2026-10-29T14:00:00Z", write_1h=100_000, output=100_000))  # $0.80 + $2.00
    assert card.current.cache_loss == pytest.approx(0.78)
    assert card.current.cache_loss_share == pytest.approx(0.78 / 3.6)


def test_cache_loss_share_is_none_without_cost(root, service_for):
    card = service_for(root).cache_card("last7", NOW)
    assert card.current.cache_loss == 0
    assert card.current.cache_loss_share is None
    assert card.delta_cache_loss_share is None


def test_cache_loss_share_change_vs_baseline(root, service_for):
    root.write_transcript("p", "s1", [
        req("a", "2026-10-20T10:00:00Z", write_1h=100_000),
        req("b", "2026-10-20T14:00:00Z", write_1h=100_000),   # baseline: loss 0.78 of 1.6
    ])
    root.write_transcript("p", "s2", [
        req("c", "2026-10-29T10:00:00Z", write_1h=100_000),   # current: no loss
    ])
    card = service_for(root).cache_card("last7", NOW)
    assert card.baseline.cache_loss_share == pytest.approx(0.78 / 1.6)
    assert card.delta_cache_loss_share == pytest.approx(0 - 0.78 / 1.6)


def test_spend_by_cache_event_dimension(root, service_for):
    root.write_transcript("p", "s1", [
        req("a", "2026-10-29T10:00:00Z", write_1h=100_000),
        req("b", "2026-10-29T10:01:00Z", cache_read=100_000, output=1_000_000),
        req("c", "2026-10-29T14:00:00Z", write_1h=100_000),
    ])
    rows = {r.event: r for r in service_for(root).spend_by_cache_event(
        date(2026, 10, 29), date(2026, 10, 29))}
    assert set(rows) == {"cold write", "warm write", "cache read"}
    assert rows["warm write"].cost == pytest.approx(0.8)
    assert rows["cold write"].cost == pytest.approx(0.8)
    assert rows["cache read"].cost == pytest.approx(100_000 * 0.2 / 1e6)
    assert rows["cold write"].tokens == 100_000


def test_classification_survives_incremental_ingest(root, tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    root.write_transcript("p", "s1", [req("a", "2026-10-30T10:00:00Z", write_1h=1_000)])
    ingest.ingest(conn, [root.path])
    root.append_to_transcript("p", "s1", [req("b", "2026-10-30T12:00:00Z", write_1h=1_000)])
    ingest.ingest(conn, [root.path])
    assert QueryService(conn, "UTC").cache_events() == {"a": "warm", "b": "cold"}


def test_dashboard_shows_cache_card(root, configure):
    root.write_transcript("p", "s1", [
        req("a", "2026-10-29T10:00:00Z", write_1h=100_000),
        req("b", "2026-10-29T14:00:00Z", write_1h=100_000),
    ])
    env = configure(root)
    cfg = cli.load_config(env)

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    html = create_app(factory, lambda: NOW).test_client().get("/").get_data(as_text=True)
    assert 'id="cache-card"' in html
    for label in ("Hit ratio", "Cold writes", "Warm writes", "5m writes", "1h writes"):
        assert label in html


def test_dashboard_kpi_row_has_a_cache_loss_tile_and_the_card_a_cache_loss_row(root, configure):
    root.write_transcript("p", "s1", [
        req("a", "2026-10-29T10:00:00Z", write_1h=100_000),
        req("b", "2026-10-29T14:00:00Z", write_1h=100_000),
    ])
    env = configure(root)
    cfg = cli.load_config(env)
    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    html = create_app(factory, lambda: NOW).test_client().get("/").get_data(as_text=True)
    kpis = html.split('class="kpis"')[1].split('class="card"')[0]
    assert "Cache loss" in kpis and "of spend" in kpis and "$0.78" in kpis
    assert "Cache hit ratio" not in kpis and "Cold writes" not in kpis
    card = html.split('id="cache-card"')[1].split("</table>")[0]
    for label in ("Hit ratio", "Cache loss", "Cold writes"):
        assert label in card


# --- Top sessions by Cache loss ---------------------------------------------------------------

def _cold_pair(root, sid, day, write, tail=(), **kw):
    """A session whose second request rebuilds `write` tokens cold (1h write $8/M, read $0.2/M)."""
    root.write_transcript("p", sid, [
        req(f"{sid}-a", f"2026-10-{day}T10:00:00Z", write_1h=write),
        req(f"{sid}-b", f"2026-10-{day}T14:00:00Z", write_1h=write, **kw), *tail])


def test_top_sessions_rank_by_cache_loss_with_cost_and_share(root, service_for):
    _cold_pair(root, "small", "29", 100_000)
    _cold_pair(root, "big", "28", 300_000, output=100_000)   # cost 2.4 + 2.4 + 2.0
    top = service_for(root).cache_card("last7", NOW).current.top_sessions
    assert [s.session_id for s in top] == ["big", "small"]
    big = top[0]
    assert big.cache_loss == pytest.approx(300_000 * 7.8 / 1e6)
    assert big.cost == pytest.approx(6.8)
    assert big.share == pytest.approx(big.cache_loss / 6.8)
    assert big.label


def test_top_sessions_are_capped_at_five(root, service_for):
    for i in range(7):
        _cold_pair(root, f"s{i}", "29", 100_000 + i * 10_000)
    top = service_for(root).cache_card("last7", NOW).current.top_sessions
    assert [s.session_id for s in top] == ["s6", "s5", "s4", "s3", "s2"]


def test_top_sessions_exclude_zero_loss_sessions(root, service_for):
    _cold_pair(root, "cold", "29", 100_000)
    root.write_transcript("p", "warm", [
        req("w1", "2026-10-29T10:00:00Z", write_1h=100_000),
        req("w2", "2026-10-29T10:01:00Z", cache_read=100_000, write_1h=1_000)])
    top = service_for(root).cache_card("last7", NOW).current.top_sessions
    assert [s.session_id for s in top] == ["cold"]


def test_top_sessions_are_empty_without_cold_writes(root, service_for):
    root.write_transcript("p", "s1", [req("a", "2026-10-29T10:00:00Z", write_1h=1_000)])
    assert service_for(root).cache_card("last7", NOW).current.top_sessions == ()


def test_subagent_cache_loss_and_cost_roll_up_into_the_owning_session(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T10:00:00Z", write_1h=100_000)])
    root.write_subagent_transcript("p", "s1", "a1", [
        req("x1", "2026-10-29T10:00:00Z", write_1h=100_000),
        req("x2", "2026-10-29T14:00:00Z", write_1h=100_000)])   # cold, inside the subagent
    top = service_for(root).cache_card("last7", NOW).current.top_sessions
    assert [s.session_id for s in top] == ["s1"]
    assert top[0].cache_loss == pytest.approx(100_000 * 7.8 / 1e6)
    assert top[0].cost == pytest.approx(3 * 0.8)


def _dashboard(root, configure):
    env = configure(root)
    cfg = cli.load_config(env)

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    return create_app(factory, lambda: NOW).test_client().get("/").get_data(as_text=True)


def test_dashboard_cache_card_lists_top_sessions_linking_to_their_timeline(root, configure):
    _cold_pair(root, "s1", "29", 100_000)
    card = _dashboard(root, configure).split('id="cache-card"')[1].split("</table>")[0]
    assert 'href="/session/s1' in card and "$0.78" in card and "of its cost" in card


def test_dashboard_has_no_top_sessions_list_without_cold_writes(root, configure):
    root.write_transcript("p", "s1", [req("a", "2026-10-29T10:00:00Z", write_1h=1_000)])
    html = _dashboard(root, configure)
    assert 'id="cache-top-sessions"' not in html
