"""Opportunity: Wide subagent. Subagents whose first request has a large context."""
from __future__ import annotations

from .core import OPPORTUNITY, Covered, Detection, FindingContext, FindingDef, Threshold

THRESHOLD = "wide_subagent_context_tokens"


def detect(ctx: FindingContext) -> Detection:
    limit = ctx.threshold(THRESHOLD)
    return Detection([Covered(r.message_id) for r in ctx.rows
                      if r.agent_id and r.stream_first_context >= limit],
                     f"first-request context >= {limit:,} tokens")


FINDING = FindingDef(
    id="wide-subagent", kind=OPPORTUNITY, title="Wide subagent",
    description="Subagents whose first request already carries a large context. "
                "May be intentional; shown for review.",
    formula=None, detect=detect,
    thresholds=(Threshold(THRESHOLD, 50_000, "Minimum first-request context (tokens) of a Wide subagent",
                          minimum=1),))
