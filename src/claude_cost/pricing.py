"""Pricing: a dated price table keyed by model prefix, $ per million tokens.

Add a price change by appending a PriceEntry with a later `effective_from`.
Cache writes are 1.25x (5m) and 2x (1h) of the input price.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

WRITE_5M_MULT = 1.25
WRITE_1H_MULT = 2.0


@dataclass(frozen=True)
class PriceEntry:
    effective_from: date
    input: float
    output: float
    cache_read: float


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


def _p(i: float, o: float, cr: float) -> list[PriceEntry]:
    return [PriceEntry(_EPOCH, i, o, cr)]


PRICE_TABLE: dict[str, list[PriceEntry]] = {
    "claude-fable-5-1": _p(10, 50, 0.25),
    "claude-fable-5": _p(10, 50, 1.0),
    "claude-opus-5-5": _p(4, 20, 0.20),
    "claude-opus-5": _p(5, 25, 0.5),
    "claude-opus-4-8": _p(5, 25, 0.5),
    "claude-opus-4-7": _p(5, 25, 0.5),
    "claude-opus-4-6": _p(5, 25, 0.5),
    "claude-sonnet-5-5": _p(2, 10, 0.10),
    "claude-sonnet-5": _p(2, 10, 0.2),
    "claude-sonnet-4-6": _p(3, 15, 0.3),
    "claude-haiku-5-5": _p(0.1, 0.5, 0.01),
    "claude-haiku-4-5": _p(1, 5, 0.1),
}


def find_price(model: str, on: date) -> PriceEntry | None:
    """Price in effect for `model` on a date; longest prefix wins. None = unknown model."""
    for prefix in sorted(PRICE_TABLE, key=len, reverse=True):
        if model.startswith(prefix):
            eligible = [e for e in PRICE_TABLE[prefix] if e.effective_from <= on]
            return max(eligible, key=lambda e: e.effective_from) if eligible else None
    return None


def price_request(model: str, on: date, *, input: int, output: int,
                  cache_read: int, write_5m: int, write_1h: int) -> CostComponents | None:
    """Cost components of one request, or None when the model is not in the table."""
    p = find_price(model, on)
    if p is None:
        return None
    m = 1_000_000
    return CostComponents(
        input=input * p.input / m,
        output=output * p.output / m,
        cache_write_5m=write_5m * p.input * WRITE_5M_MULT / m,
        cache_write_1h=write_1h * p.input * WRITE_1H_MULT / m,
        cache_read=cache_read * p.cache_read / m,
    )
