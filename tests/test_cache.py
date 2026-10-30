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
    for label in ("Hit ratio", "Cold writes", "Warm writes", "5m writes", "1h writes", "Rebuilds"):
        assert label in html
