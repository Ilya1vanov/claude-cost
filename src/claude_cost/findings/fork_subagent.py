"""Opportunity: Fork subagent. Forked Subagents (fork flag in subagent metadata)."""
from __future__ import annotations

from .core import OPPORTUNITY, Covered, Detection, FindingContext, FindingDef


def detect(ctx: FindingContext) -> Detection:
    return Detection([Covered(r.message_id) for r in ctx.period_rows if r.agent_id and r.agent_fork])


FINDING = FindingDef(
    id="fork-subagent", kind=OPPORTUNITY, title="Fork subagent",
    description="Forked Subagents, which inherit the parent's context. May be intentional; shown for review.",
    formula=None, detect=detect)
