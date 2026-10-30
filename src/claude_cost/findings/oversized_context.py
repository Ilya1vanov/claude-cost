"""Drain: Oversized context. Requests above the learned Compact threshold."""
from __future__ import annotations

from .. import compact
from .core import DRAIN, Covered, Detection, FindingContext, FindingDef


def detect(ctx: FindingContext) -> Detection:
    analysis = ctx.compact
    if analysis is None or analysis.global_ is None:
        return Detection([], "no compactions observed, so no Compact threshold")
    covered, charged = [], set()
    for r in ctx.period_rows:
        t = analysis.for_model(r.model)
        price = compact.read_price(r.model, r.day, ctx.price_table)
        if t is None or price is None or r.context <= t.threshold:
            continue
        avoidable = (r.context - t.threshold) * price  # reads above T
        stream = analysis.stream_of.get(r.message_id, (r.session_id, None))
        if stream not in charged:  # one compaction per stream pays for all its excess reads
            charged.add(stream)
            avoidable -= t.compaction_cost
        covered.append(Covered(r.message_id, avoidable))
    g = analysis.global_
    return Detection(covered, f"global Compact threshold {g.threshold:,} tokens "
                              f"({g.sample_size} compactions)")


FINDING = FindingDef(
    id="oversized-context", kind=DRAIN, title="Oversized context",
    description="Requests whose context is above the Compact threshold learned from your "
                "history, whether or not the session later compacts.",
    formula="avoidable = reads of the context above the Compact threshold - the cost of "
            "compacting at it (once per session or subagent)",
    detect=detect)
