"""Block pricing: what a fixed block of tokens sitting in a Stream cost. `Stream.charge` prices a
block from its start to its end (or the Stream's end) over a Stream's Period requests: reads on the
later requests, the writer's write when it is a Period request, Cold-write rebuilds, and (via
`split_tier_drops`) Prompt tier drop. Findings that price a block of tokens share it."""
from __future__ import annotations

from bisect import bisect_left
from itertools import accumulate

from .. import pricing
from ..cache import COLD
from .core import Covered, FindingContext


def _price(ctx: FindingContext, r, *, input: int, output: int, cache_read: int,
           write_5m: int, write_1h: int):
    """Request `r` priced with the given token counts and its own model, day, speed and geo."""
    return pricing.price_request(r.model, r.day, input=input, output=output, cache_read=cache_read,
                                 write_5m=write_5m, write_1h=write_1h, speed=r.speed,
                                 inference_geo=r.inference_geo, table=ctx.price_table)


def _write_rate(ctx: FindingContext, r) -> float:
    """Cache write $ per token for request `r`: its own 5m : 1h write mix, at its own prompt
    size (so any Prompt tier applies). 0 when it wrote nothing."""
    writes = r.write_5m + r.write_1h
    if not writes:
        return 0.0
    c = _price(ctx, r, input=r.input, output=0, cache_read=r.cache_read,
               write_5m=r.write_5m, write_1h=r.write_1h)
    return (c.cache_write_5m + c.cache_write_1h) / writes if c else 0.0


class Stream:
    """One (session, agent) stream's period requests, oldest first, with each request's read
    rate and the unused Listing tokens it reads (kept as a difference array).

    `extra[i]` is what the upper bound adds to request i beyond its reads: the write of the
    Listings it wrote, the rebuild of Listings it re-wrote as a Cold write, and its Prompt tier
    drop. All add into the same array, via `_add_upper`."""

    def __init__(self, ctx: FindingContext, reqs: list):
        self.ctx = ctx
        self.reqs = reqs
        # (first, end, tokens, key) per charged Listing: its reads, and the writer's own write
        self.reads: list[tuple[int, int, int, tuple]] = []
        self.sitting = [0] * (len(reqs) + 1)  # difference array of reads + writes: Listing tokens in a prompt
        self.ts = [r.ts for r in reqs]
        self.rates = [pricing.read_rate(r, ctx.price_table) for r in reqs]
        self.write_rates = [_write_rate(ctx, r) for r in reqs]
        self.cold = [i for i, r in enumerate(reqs) if r.cache_event == COLD]  # indices, ascending
        self.extra = [0.0] * len(reqs)
        self.delta = [0] * (len(reqs) + 1)
        # the Stream's last Request before the Period, if any (its gap is stored at ingest)
        self.prev_ts = None if reqs[0].gap is None else reqs[0].ts - reqs[0].gap

    def charge(self, ts: float, gone: float | None, tokens: int,
               key: tuple = ()) -> tuple[float, float] | None:
        """Charge a Listing written at `ts` (removed at `gone`) to the requests that read it from
        the cache: those after the one that wrote it (the first at or after `ts`, possibly before
        the Period), before the removal. Returns its (lower, upper) $, or None when no request
        read it. The upper bound adds the write when the writer is a Period Request, and swaps the
        read for the write on each Cold-write Request in the range."""
        lo = bisect_left(self.ts, ts)
        # a Period Request precedes the Listing, or no Request between the Listing and the
        # Period does: either way the writer is self.reqs[lo]
        writer_in_period = lo > 0 or self.prev_ts is None or self.prev_ts < ts
        writer = lo if writer_in_period else None
        if writer_in_period:
            lo += 1  # skip the writer
        hi = len(self.reqs) if gone is None else bisect_left(self.ts, gone)
        if writer is not None and writer < hi:
            # the Listing sits in the writer's prompt, read or not: it leaves it too (tier drop)
            self.reads.append((writer, writer + 1, tokens, key))
            self.sitting[writer] += tokens
            self.sitting[writer + 1] -= tokens
        if lo >= hi:
            return None
        self.delta[lo] += tokens
        self.delta[hi] -= tokens
        self.reads.append((lo, hi, tokens, key))
        self.sitting[lo] += tokens
        self.sitting[hi] -= tokens
        write = 0.0
        if writer is not None:  # read at least once, so its write was avoidable too (upper bound)
            write = self._written(writer, tokens) * self.write_rates[writer]
            self._add_upper(writer, write)
        # a Cold write rebuilds the Listing too: what it wrote pays the write in place of the read
        for j in self.cold[bisect_left(self.cold, lo):bisect_left(self.cold, hi)]:
            rebuild = self._written(j, tokens) * (self.write_rates[j] - self.rates[j])
            self._add_upper(j, rebuild)
            write += rebuild
        # summed, not prefix-subtracted, so equal reads give equal $ and ties sort by name
        reads = tokens * sum(self.rates[lo:hi])
        return reads, reads + write

    def _written(self, i: int, tokens: int) -> int:
        """How many of a Listing's `tokens` request i can have written: no more than it wrote (the
        rest came from the cache)."""
        r = self.reqs[i]
        return min(tokens, r.write_5m + r.write_1h)

    def _add_upper(self, i: int, dollars: float) -> None:
        self.extra[i] += dollars

    def _tier_drop(self, i: int, unused: int) -> float:
        """What request i's whole cost loses when `unused` tokens leave its prompt and that takes
        it from above its model's Prompt tier threshold to at or below it; else 0."""
        r = self.reqs[i]
        p = pricing.find_price(r.model, r.day, self.ctx.price_table)
        if p is None or p.tier_threshold is None or not r.context - unused <= p.tier_threshold < r.context:
            return 0.0
        c = _price(self.ctx, r, input=r.input, output=r.output, cache_read=r.cache_read,
                   write_5m=r.write_5m, write_1h=r.write_1h)
        return c.total * (p.tier_mult - 1) / p.tier_mult

    def split_tier_drops(self) -> list[tuple[tuple, float]]:
        """Add each request's Prompt tier drop to its upper bound, once, and return it split across
        the unused Listings it reads, by token share, as (key, dollars). Call after every `charge`."""
        unused = list(accumulate(self.sitting))
        drops = {i: d for i, t in enumerate(unused) if t and (d := self._tier_drop(i, t))}
        for i, d in drops.items():
            self._add_upper(i, d)
        at = sorted(drops)
        return [(key, drops[i] * tokens / unused[i])
                for lo, hi, tokens, key in self.reads
                for i in at[bisect_left(at, lo):bisect_left(at, hi)]]

    def covered(self) -> list[Covered]:
        out = []
        for r, rate, tokens, extra in zip(self.reqs, self.rates, accumulate(self.delta), self.extra):
            if tokens or extra:
                low = tokens * rate
                out.append(Covered(r.message_id, low, low + extra))
        return out
