"""Drain: Cold resume. A cold cache write after a long idle gap on a large context."""
from __future__ import annotations

from .core import DRAIN, Covered, Detection, FindingContext, FindingDef, Threshold


def detect(ctx: FindingContext) -> Detection:
    idle = ctx.threshold("cold_resume_idle_minutes") * 60
    min_ctx = ctx.threshold("cold_resume_min_context")
    fresh = ctx.median_first_context()
    covered = []
    for r in ctx.period_rows:
        if r.cache_event != "cold" or r.gap is None or r.gap < idle or r.context < min_ctx:
            continue
        written = r.write_5m + r.write_1h
        # a fresh session would write the median first-request context at this request's write price
        fresh_cost = fresh * r.write_cost / written if written else 0.0
        covered.append(Covered(r.message_id, r.write_cost - fresh_cost))
    return Detection(covered, f"median first-request context: {fresh:,.0f} tokens")


FINDING = FindingDef(
    id="cold-resume", kind=DRAIN, title="Cold resume",
    description="Requests after a long idle gap whose prompt cache expired, so the whole "
                "context was written again.",
    formula="avoidable = cold write cost - (median first-request context x the request's "
            "cache write price per token)",
    detect=detect,
    thresholds=(
        Threshold("cold_resume_idle_minutes", 60, "Cold resume: minimum idle minutes before the request.", 1),
        Threshold("cold_resume_min_context", 100_000, "Cold resume: minimum context size in tokens.", 1),
    ))
