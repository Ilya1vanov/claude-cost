"""Pricing: a dated price table keyed by model prefix, $ per million tokens.

Add a price change by appending a PriceEntry with a later `effective_from`.
Cache writes are 1.25x (5m) and 2x (1h) of the input price. Cache reads use a per-model
multiplier of the input price (see `_p`). Modifiers applied in order: prompt-size tier,
fast mode (FAST_MULT on every price), US-only inference (US_MULT on every Cost component).
Config overrides (`build_table`) replace a model's entries; unknown models are not priced.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping

WRITE_5M_MULT = 1.25
WRITE_1H_MULT = 2.0
FAST_MULT = 2.0  # fast mode: every price x2; cache multipliers then apply to the fast input price
US_MULT = 1.1  # inference_geo "us": every Cost component x1.1
DEFAULT_READ_MULT = 0.1


@dataclass(frozen=True)
class PriceEntry:
    effective_from: date
    input: float
    output: float
    cache_read: float
    tier_threshold: int | None = None  # prompt tokens above this use `tier_mult` x prices
    tier_mult: float = 1.0
    source: str = "default"  # "config" for overrides


@dataclass(frozen=True)
class CostComponents:
    input: float = 0.0
    output: float = 0.0
    cache_write_5m: float = 0.0
    cache_write_1h: float = 0.0
    cache_read: float = 0.0

    @property
    def total(self) -> float:
        return (self.input + self.output + self.cache_write_5m
                + self.cache_write_1h + self.cache_read)


_EPOCH = date(2020, 1, 1)


def _p(i: float, o: float, read_mult: float = DEFAULT_READ_MULT, **kw) -> list[PriceEntry]:
    """Input $/M, output $/M, cache-read multiplier of input (0.025 Fable 5.1, 0.05 Opus/Sonnet 5.5)."""
    return [PriceEntry(_EPOCH, i, o, round(i * read_mult, 6), **kw)]


PRICE_TABLE: dict[str, list[PriceEntry]] = {
    "claude-fable-5-1": _p(10, 50, 0.025),
    "claude-fable-5": _p(10, 50),
    "claude-opus-5-5": _p(4, 20, 0.05),
    "claude-opus-5": _p(5, 25),
    "claude-opus-4-8": _p(5, 25),
    "claude-opus-4-7": _p(5, 25),
    "claude-opus-4-6": _p(5, 25),
    "claude-sonnet-5-5": _p(2, 10, 0.05),
    "claude-sonnet-5": _p(2, 10),
    "claude-sonnet-4-6": _p(3, 15),
    "claude-haiku-5-5": _p(0.1, 0.5, tier_threshold=100_000, tier_mult=2.0),  # tier price assumed 2x
    "claude-haiku-4-5": _p(1, 5),
}


def build_table(overrides: Mapping[str, Mapping[str, float]] | None = None
                ) -> dict[str, list[PriceEntry]]:
    """PRICE_TABLE with config overrides: {model prefix: {input, output, cache_read?}} per million.

    An override replaces all entries of that prefix (adds the model if new) and drops tiers.
    `cache_read` defaults to 0.1x the input price.
    """
    table = dict(PRICE_TABLE)
    for model, o in (overrides or {}).items():
        cr = o.get("cache_read", o["input"] * DEFAULT_READ_MULT)
        table[model] = [PriceEntry(_EPOCH, o["input"], o["output"], cr, source="config")]
    return table


def find_price(model: str, on: date,
               table: Mapping[str, list[PriceEntry]] | None = None) -> PriceEntry | None:
    """Price in effect for `model` on a date; longest prefix wins. None = unknown model."""
    table = PRICE_TABLE if table is None else table
    for prefix in sorted(table, key=len, reverse=True):
        if model.startswith(prefix):
            eligible = [e for e in table[prefix] if e.effective_from <= on]
            return max(eligible, key=lambda e: e.effective_from) if eligible else None
    return None


def price_request(model: str, on: date, *, input: int, output: int,
                  cache_read: int, write_5m: int, write_1h: int, speed: str | None = None,
                  inference_geo: str | None = None,
                  table: Mapping[str, list[PriceEntry]] | None = None) -> CostComponents | None:
    """Cost components of one request, or None when the model is not in the table."""
    p = find_price(model, on, table)
    if p is None:
        return None
    mult = 1.0
    if p.tier_threshold is not None and input + cache_read + write_5m + write_1h > p.tier_threshold:
        mult *= p.tier_mult
    if speed == "fast":
        mult *= FAST_MULT
    if inference_geo == "us":
        mult *= US_MULT
    m = 1_000_000 / mult
    return CostComponents(
        input=input * p.input / m,
        output=output * p.output / m,
        cache_write_5m=write_5m * p.input * WRITE_5M_MULT / m,
        cache_write_1h=write_1h * p.input * WRITE_1H_MULT / m,
        cache_read=cache_read * p.cache_read / m,
    )
