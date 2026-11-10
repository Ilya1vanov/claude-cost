"""Drain: Oversized context. Requests above the learned Compact threshold."""
from __future__ import annotations

from .. import compact
from ..cache import COLD
from .core import DRAIN, Covered, Detection, FindingContext, FindingDef


def detect(ctx: FindingContext) -> Detection:
    analysis = ctx.compact
    if analysis is None or analysis.global_ is None:
        return Detection([], "no compactions observed, so no Compact threshold")
    covered, charged = [], set()
    for r in ctx.rows:
        t = analysis.for_model(r.model)
        rates = compact.rates(r.model, r.day, r.speed, r.inference_geo, ctx.price_table)
        if t is None or rates is None or r.context <= t.threshold:
            continue
        # reads above T; at T the context may be back under the Prompt tier
        read_cost = rates.read_cost(r.context)
        avoidable = read_cost - rates.read_cost(t.threshold)
        stream = (r.session_id, r.agent_id)
        if stream not in charged:  # one compaction per stream pays for all its excess reads
            charged.add(stream)
            avoidable -= rates.compaction_cost(r.context, t.post_tokens_p75)  # as replay does
        high = avoidable
        written = r.write_5m + r.write_1h
        if r.cache_event == COLD and written > 0:
            # at T the Cold write would have rebuilt only T tokens: swap the excess's read for its write
            excess = min(r.context - t.threshold, written)
            read_price = read_cost / r.context
            high += excess * (r.write_cost / written - read_price)
        covered.append(Covered(r.message_id, avoidable, high))
    g = analysis.global_
    return Detection(covered, f"global Compact threshold {g.threshold:,} tokens "
                              f"({g.sample_size} compactions)")


FINDING = FindingDef(
    id="oversized-context", kind=DRAIN, title="Oversized context",
    description="Requests whose context is above the Compact threshold learned from your "
                "history, whether or not the session later compacts.",
    formula="avoidable = reads of the context - reads of the Compact threshold - the cost of "
            "compacting at it (once per session or subagent), at the request's Prompt tier, "
            "speed and region; the upper bound also swaps the read price for the write price on "
            "the excess over the Compact threshold that each Cold write rebuilt (at most the "
            "tokens it wrote)",
    detect=detect)
