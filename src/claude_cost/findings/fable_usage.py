"""Opportunity: Fable usage. Requests on a Fable model, compared with Opus 5.5 prices."""
from __future__ import annotations

from .core import OPPORTUNITY, Covered, Detection, FindingContext, FindingDef


def detect(ctx: FindingContext) -> Detection:
    covered = []
    for r in ctx.rows:
        if not r.model.startswith("claude-fable"):
            continue
        alt = ctx.price(ctx.compare_model, r)
        covered.append(Covered(r.message_id, r.cost - r.web_search_cost - alt.total if alt else 0.0))
    return Detection(covered)


FINDING = FindingDef(
    id="fable-usage", kind=OPPORTUNITY, title="Fable usage",
    description="Requests that ran on a Fable model. May be intentional; shown for review.",
    formula="avoidable = Fable cost - the same tokens at the comparison model's prices "
            "(Opus 5.5 unless fable_compare_model is set)",
    detect=detect)
