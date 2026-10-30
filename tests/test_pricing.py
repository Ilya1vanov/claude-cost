"""Seam 1: pricing cases, asserted through the query service."""
from __future__ import annotations

from datetime import date

import pytest

from claude_cost import pricing

from helpers import req

DAY = date(2026, 10, 30)
TS = "2026-10-30T01:00:00Z"
M = 1_000_000


def total(svc) -> float:
    return svc.daily_spend(DAY, DAY)[0].cost


def one(root, service_for, **kw):
    root.write_transcript("p", "s1", [req("m1", TS, **kw)])
    return service_for(root)


def test_haiku_55_over_100k_prompt_uses_higher_tier(root, service_for):
    # 100_000 prompt tokens: base tier. input $0.10/M -> $0.01; output $0.50/M on 1M -> $0.50
    base = one(root, service_for, model="claude-haiku-5-5", input=100_000, output=M)
    assert total(base) == pytest.approx(0.01 + 0.50)


def test_haiku_55_prompt_counts_cache_tokens(root, service_for):
    # 100_001 prompt tokens (cache read counts): higher tier = 5x all prices ($0.50/$2.50 per M, claude-api skill pricing table)
    svc = one(root, service_for, model="claude-haiku-5-5", cache_read=100_001, output=M)
    assert total(svc) == pytest.approx(100_001 * 0.01 * 5 / M + 0.50 * 5)


def test_fast_mode_uses_fast_prices_with_cache_multipliers_on_top(root, service_for):
    # opus-5-5 $4 in / $20 out / read 0.05x; fast = 2x. Each component 1M tokens.
    svc = one(root, service_for, speed="fast", input=M, output=M, write_5m=M, write_1h=M,
              cache_read=M)
    standard = 4 + 20 + 4 * 1.25 + 4 * 2 + 0.2
    assert total(svc) == pytest.approx(2 * standard)


def test_us_inference_applies_1_1x_to_every_component(root, service_for):
    kw = dict(input=M, output=M, write_5m=M, write_1h=M, cache_read=M)
    us = one(root, service_for, inference_geo="us", **kw)
    assert total(us) == pytest.approx(1.1 * (4 + 20 + 5 + 8 + 0.2))


@pytest.mark.parametrize("model, per_mtok", [
    ("claude-fable-5-1", 0.25),    # 0.025x of $10
    ("claude-opus-5-5", 0.20),     # 0.05x of $4
    ("claude-sonnet-5-5", 0.10),   # 0.05x of $2
    ("claude-opus-4-8", 0.50),     # 0.1x of $5
    ("claude-sonnet-4-6", 0.30),
])
def test_cache_reads_use_per_model_multiplier(root, service_for, model, per_mtok):
    assert total(one(root, service_for, model=model, cache_read=M)) == pytest.approx(per_mtok)


def test_dated_price_change_prices_before_and_after_differently(root, service_for, monkeypatch):
    monkeypatch.setitem(pricing.PRICE_TABLE, "claude-opus-5-5", [
        pricing.PriceEntry(date(2020, 1, 1), 4, 20, 0.2),
        pricing.PriceEntry(date(2026, 10, 30), 8, 40, 0.4)])
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T23:00:00Z", output=M),
                                      req("m2", "2026-10-30T01:00:00Z", output=M)])
    svc = service_for(root)
    assert svc.daily_spend(date(2026, 10, 29), date(2026, 10, 29))[0].cost == pytest.approx(20)
    assert total(svc) == pytest.approx(40)


def test_unknown_model_is_flagged_not_priced(root, service_for):
    svc = one(root, service_for, model="claude-mystery-9", output=M)
    assert total(svc) == 0
    assert svc.unpriced_models() == ["claude-mystery-9"]


def test_config_price_override_takes_effect_and_adds_models(root, service_for):
    root.write_transcript("p", "s1", [req("m1", TS, output=M, cache_read=M),
                                      req("m2", TS, model="claude-mystery-9", input=M)])
    svc = service_for(root, prices={"claude-opus-5-5": {"input": 1, "output": 3, "cache_read": 0.5},
                                    "claude-mystery-9": {"input": 7, "output": 9}})
    assert total(svc) == pytest.approx(3 + 0.5 + 7)
    assert svc.unpriced_models() == []


def test_override_reprices_existing_requests_on_next_ingest(root, tmp_path):
    from claude_cost import ingest, store
    from claude_cost.query import QueryService
    root.write_transcript("p", "s1", [req("m1", TS, output=M)])
    conn = store.connect(tmp_path / "s.sqlite")
    t1 = pricing.build_table(None)
    ingest.ingest(conn, [root.path], price_table=t1)
    assert total(QueryService(conn, "UTC", price_table=t1)) == pytest.approx(20)
    t2 = pricing.build_table({"claude-opus-5-5": {"input": 1, "output": 3}})
    ingest.ingest(conn, [root.path], price_table=t2)
    assert total(QueryService(conn, "UTC", price_table=t2)) == pytest.approx(3)


def test_price_table_in_effect_marks_overrides(root, service_for):
    svc = service_for(root, prices={"claude-opus-5-5": {"input": 1, "output": 3}})
    rows = {r.model: r for r in svc.price_table()}
    assert rows["claude-opus-5-5"].source == "config" and rows["claude-opus-5-5"].input == 1
    assert rows["claude-opus-5-5"].cache_read == pytest.approx(0.1)  # default 0.1x input
    assert rows["claude-sonnet-5-5"].source == "default"
    assert rows["claude-haiku-5-5"].tier_note
