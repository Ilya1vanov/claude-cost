"""Findings registry. Every module in this package that defines `FINDING` (a `FindingDef`)
is registered automatically: add a Drain or Opportunity by adding one module here."""
from __future__ import annotations

import importlib
import pkgutil

from .core import (DRAIN, NOT_ADDITIVE_NOTE, OPPORTUNITY, Covered, Detection, Finding,  # noqa: F401
                   CORE_THRESHOLDS, DEFAULT_COMPARE_MODEL, FindingContext, FindingDef, RequestRow, Threshold, build_finding,
                   format_avoidable, load_rows)


def all_findings() -> list[FindingDef]:
    """Registered definitions, Drains first, then by id."""
    defs = []
    for m in pkgutil.iter_modules(__path__):
        if m.name == "core":
            continue
        d = getattr(importlib.import_module(f"{__name__}.{m.name}"), "FINDING", None)
        if isinstance(d, FindingDef):
            defs.append(d)
    ids = [d.id for d in defs]
    if len(ids) != len(set(ids)):
        raise RuntimeError(f"duplicate finding ids: {sorted(ids)}")
    return sorted(defs, key=lambda d: (d.kind != DRAIN, d.id))


def all_thresholds() -> dict[str, Threshold]:
    return {t.name: t for t in (*CORE_THRESHOLDS, *(t for d in all_findings() for t in d.thresholds))}
