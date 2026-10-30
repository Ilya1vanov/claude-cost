"""Opportunity: Wide subagent. Subagents whose first request has a large context."""
from __future__ import annotations

from .core import OPPORTUNITY, Covered, Detection, FindingContext, FindingDef, Threshold

THRESHOLD = "wide_subagent_context_tokens"


def detect(ctx: FindingContext) -> Detection:
    limit = ctx.threshold(THRESHOLD)
    first: dict[str, int] = {}  # agent_id -> context of its first request (rows are time-ordered)
    for r in ctx.rows:
        if r.agent_id and r.agent_id not in first:
            first[r.agent_id] = r.context
    return Detection([Covered(r.message_id) for r in ctx.period_rows
                      if r.agent_id and first[r.agent_id] >= limit],
                     f"first-request context >= {limit:,} tokens")


FINDING = FindingDef(
    id="wide-subagent", kind=OPPORTUNITY, title="Wide subagent",
    description="Subagents whose first request already carries a large context. "
                "May be intentional; shown for review.",
    formula=None, detect=detect,
    thresholds=(Threshold(THRESHOLD, 50_000, "Minimum first-request context (tokens) of a Wide subagent",
                          minimum=1),))
