"""Compact threshold: the context size above which compacting costs less than carrying on.

Learned per model from the user's own history. For every candidate threshold T the observed
requests are replayed: whenever a stream's context (a Session's or one Subagent's) would
exceed T it is compacted down to `postTokens` (p75 of the model's observed compactions) and
later requests keep their observed growth on top of that. Total cost(T) = reads of the
(possibly reduced) context + the cost of each compaction. T minimises it. A model with fewer
than MIN_SAMPLE compactions uses the global T (all models, same method).

Assumptions (stated in the UI): a request's carrying cost is its context at the cache-read
price; a compaction costs `postTokens` of summary output plus writing `postTokens` at the
1h cache-write price.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Mapping

from . import pricing
from .localtime import price_day
from .sql import CONTEXT_SQL, SESSION_ID_SQL, SESSION_JOIN

MIN_SAMPLE = 20  # compactions a model needs before it gets its own threshold
STEP = 10_000  # candidate thresholds are multiples of this many tokens


@dataclass(frozen=True)
class CurvePoint:
    threshold: int
    cost: float  # total replayed cost if compacting at this threshold


@dataclass(frozen=True)
class ModelThreshold:
    model: str | None  # None = the global threshold
    threshold: int
    source: str  # "model" | "global" (fallback: fewer than MIN_SAMPLE compactions)
    sample_size: int  # observed compactions of this model (all models for global)
    post_tokens_p75: int
    compaction_cost: float  # per compaction, at p75 postTokens
    payback_requests: float | None  # requests until one compaction at T has paid for itself
    actual_cost: float  # observed carrying cost of the replayed requests
    curve: tuple[CurvePoint, ...]  # of the model, or of the global fit when source == "global"


@dataclass(frozen=True)
class _Req:
    message_id: str
    stream: tuple[str, str | None]
    ts: float
    model: str
    context: int
    read_price: float  # $ per token of context
    cost_per_post: float  # $ per post token of one compaction on this model


@dataclass(frozen=True)
class CompactAnalysis:
    models: tuple[ModelThreshold, ...]  # models with at least one observed compaction
    global_: ModelThreshold | None  # None when there are no compactions at all
    stream_of: Mapping[str, tuple[str, str | None]]  # message_id -> (logical session, agent id)

    def for_model(self, model: str) -> ModelThreshold | None:
        for m in self.models:
            if m.model == model:
                return m
        return self.global_


def p75(values: list[int]) -> int:
    s = sorted(values)
    return s[math.ceil(0.75 * len(s)) - 1]


def replay(reqs: list[_Req], threshold: int, post: int) -> float:
    """Total cost of `reqs` (oldest first) if each stream compacted whenever above `threshold`."""
    offset: dict[tuple, int] = {}
    total = 0.0
    for r in reqs:
        ctx = r.context - offset.get(r.stream, 0)
        if ctx > threshold:
            offset[r.stream] = offset.get(r.stream, 0) + ctx - post
            ctx = post
            total += post * r.cost_per_post
        total += ctx * r.read_price
    return total


def _fit(model: str | None, reqs: list[_Req], posts: list[int], source: str,
         step: int = STEP) -> ModelThreshold:
    post = p75(posts)
    actual = sum(r.context * r.read_price for r in reqs)
    top = max((r.context for r in reqs), default=0)
    lo = (post // step + 1) * step  # a threshold must be above what a compaction leaves
    hi = max(lo, math.ceil(top / step) * step + step)
    curve = tuple(CurvePoint(t, replay(reqs, t, post)) for t in range(lo, hi + 1, step))
    best = min(curve, key=lambda c: (c.cost, -c.threshold))  # tie: do not compact earlier
    sample = [r for r in reqs]
    if sample:
        cost_per_post = sum(r.cost_per_post for r in sample) / len(sample)
        read = sum(r.read_price for r in sample) / len(sample)
    else:
        cost_per_post = read = 0.0
    saved_per_request = (best.threshold - post) * read
    return ModelThreshold(
        model, best.threshold, source, len(posts), post, post * cost_per_post,
        post * cost_per_post / saved_per_request if saved_per_request > 0 else None,
        actual, curve)


def analyze(conn: sqlite3.Connection, table=None, min_sample: int = MIN_SAMPLE,
            step: int = STEP) -> CompactAnalysis:
    rows = conn.execute(
        f"SELECT r.message_id, {SESSION_ID_SQL} AS sid, a.agent_id, "
        f"r.ts_epoch, r.model, {CONTEXT_SQL} AS ctx FROM requests r "
        f"{SESSION_JOIN} "
        "LEFT JOIN request_surface a ON a.message_id = r.message_id "
        "ORDER BY r.ts_epoch, r.message_id").fetchall()
    reqs: list[_Req] = []
    last_model: dict[str, list[tuple[float, str]]] = {}
    for x in rows:
        p = pricing.find_price(x["model"], price_day(x["ts_epoch"]), table)
        if p is None:
            continue
        out_per_token = (p.output + p.input * pricing.WRITE_1H_MULT) / 1_000_000
        reqs.append(_Req(x["message_id"], (x["sid"], x["agent_id"]), x["ts_epoch"], x["model"],
                         x["ctx"], p.cache_read / 1_000_000, out_per_token))
        if x["agent_id"] is None:
            last_model.setdefault(x["sid"], []).append((x["ts_epoch"], x["model"]))
    stream_of = {r.message_id: r.stream for r in reqs}

    posts: dict[str, list[int]] = {}
    for c in conn.execute(
            "SELECT c.ts_epoch, c.post_tokens, COALESCE(l.logical_session_id, c.stem) AS sid "
            "FROM compactions c LEFT JOIN session_links l ON l.stem = c.stem ORDER BY c.ts_epoch"):
        before = [m for t, m in last_model.get(c["sid"], []) if t <= c["ts_epoch"]]
        if before:  # a compaction belongs to the model of the request just before it
            posts.setdefault(before[-1], []).append(c["post_tokens"])
    all_posts = [p for ps in posts.values() for p in ps]
    if not all_posts:
        return CompactAnalysis((), None, stream_of)

    global_ = _fit(None, reqs, all_posts, "global", step)
    models = []
    for model, ps in sorted(posts.items()):
        if len(ps) >= min_sample:
            models.append(_fit(model, [r for r in reqs if r.model == model], ps, "model", step))
        else:
            own = _fit(model, [r for r in reqs if r.model == model], ps, "model", step)
            models.append(ModelThreshold(
                model, global_.threshold, "global", len(ps), own.post_tokens_p75,
                own.compaction_cost, own.payback_requests, own.actual_cost, global_.curve))
    return CompactAnalysis(tuple(models), global_, stream_of)


def read_price(model: str, day: date, table=None) -> float | None:
    p = pricing.find_price(model, day, table)
    return None if p is None else p.cache_read / 1_000_000
