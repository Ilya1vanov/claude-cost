"""Opportunity: Fable usage. Requests on a Fable model, compared with Opus 5.5 prices."""
from __future__ import annotations

from .core import OPPORTUNITY, Covered, Detection, FindingContext, FindingDef

COMPARE_MODEL = "claude-opus-5-5"


def detect(ctx: FindingContext) -> Detection:
    covered = []
    for r in ctx.period_rows:
        if not r.model.startswith("claude-fable"):
            continue
        alt = ctx.price(COMPARE_MODEL, r)
        covered.append(Covered(r.message_id, r.cost - alt.total if alt else 0.0))
    return Detection(covered)


FINDING = FindingDef(
    id="fable-usage", kind=OPPORTUNITY, title="Fable usage",
    description="Requests that ran on a Fable model. May be intentional; shown for review.",
    formula="avoidable = Fable cost - the same tokens at Opus 5.5 prices",
    detect=detect)
