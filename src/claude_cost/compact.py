"""Compact threshold: the context size above which compacting costs less than carrying on.

Learned per model from the user's own history. For every candidate threshold T the observed
requests are replayed: whenever a stream's context (a Session's or one Subagent's) would
exceed T it is compacted down to `postTokens` (p75 of the model's observed compactions) and
later requests keep their observed growth on top of that. Total cost(T) = reads of the
(possibly reduced) context + the cost of each compaction. T minimises it. A model with fewer
than MIN_SAMPLE compactions uses the global T (all models, same method).

Assumptions (stated in the UI): a request's carrying cost is its context at the cache-read
price; a compaction costs `postTokens` of summary output plus writing `postTokens` at the
1h cache-write price. Both at the request's speed and region, and at the Prompt tier of the
context read or compacted, so compacting can take a stream back under the tier.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from enum import Enum
from functools import cached_property
from typing import ClassVar, Mapping

from . import pricing
from .localtime import price_day
from .sql import CONTEXT_SQL, SESSION_ID_SQL, SESSION_JOIN

MIN_SAMPLE = 20  # compactions a model needs before it gets its own threshold
STEP = 10_000  # candidate thresholds are multiples of this many tokens
FORMAT = 2  # shape and method of the stored analysis (`to_json`); bump it whenever either changes
# (prices and price modifiers need no bump: `pricing.fingerprint` is part of the key)

# Autocompact fires at autoCompactWindow - 33,000: the window minus an output reserve, minus a
# buffer. Read from the Claude Code 2.1.314 bundle; re-check when autocompacts fire elsewhere.
CLAUDE_CODE_VERSION = "2.1.314"
AUTOCOMPACT_OUTPUT_RESERVE = 20_000
AUTOCOMPACT_BUFFER = 13_000
AUTOCOMPACT_OFFSET = AUTOCOMPACT_OUTPUT_RESERVE + AUTOCOMPACT_BUFFER
MIN_AUTOCOMPACT_WINDOW = 100_000  # the range Claude Code accepts for autoCompactWindow
MAX_AUTOCOMPACT_WINDOW = 1_000_000


@dataclass(frozen=True)
class CurvePoint:
    threshold: int
    cost: float  # total replayed cost if compacting at this threshold


class WindowClamp(Enum):
    RAISED = "raised"
    LOWERED = "lowered"


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

    @cached_property
    def _window(self) -> tuple[int, WindowClamp | None]:
        wanted = self.threshold + AUTOCOMPACT_OFFSET
        if wanted < MIN_AUTOCOMPACT_WINDOW:
            return MIN_AUTOCOMPACT_WINDOW, WindowClamp.RAISED
        if wanted > MAX_AUTOCOMPACT_WINDOW:
            return MAX_AUTOCOMPACT_WINDOW, WindowClamp.LOWERED
        return wanted, None

    @property
    def autocompact_window(self) -> int:
        """modelSettings.<model>.autoCompactWindow that makes autocompact fire at `threshold`."""
        return self._window[0]

    @property
    def window_clamp(self) -> WindowClamp | None:
        """How `autocompact_window` had to be clamped to Claude Code's range, else None."""
        return self._window[1]

    @property
    def window_note(self) -> str | None:
        if self.window_clamp is WindowClamp.RAISED:
            return (f"raised to {MIN_AUTOCOMPACT_WINDOW:,}: autocompact fires at "
                    f"{MIN_AUTOCOMPACT_WINDOW - AUTOCOMPACT_OFFSET:,}")
        if self.window_clamp is WindowClamp.LOWERED:
            return f"lowered to {MAX_AUTOCOMPACT_WINDOW:,}: autocompact can't reach the Compact threshold"
        return None


@dataclass(frozen=True)
class Rates:
    """A request's prices at its speed and region; a context above `tier_threshold` pays
    `tier_mult` x them (its Prompt tier)."""
    read: float  # $ per token of context
    output: float  # $ per post token of compaction summary output
    write: float  # $ per post token written to the 1h cache after a compaction
    tier_threshold: int | None = None
    tier_mult: float = 1.0

    def read_cost(self, context: int) -> float:
        return context * self.read * pricing.tier(self, context)

    def compaction_cost(self, context: int, post: int) -> float:
        """Compacting `context` down to `post`: the summary request's prompt is `context`, so its
        output pays that tier; the write lands in a prompt of `post`."""
        return post * (self.output * pricing.tier(self, context) + self.write * pricing.tier(self, post))


def rates(model: str, day: date, speed: str | None = None, inference_geo: str | None = None,
          table=None) -> Rates | None:
    p = pricing.find_price(model, day, table)
    if p is None:
        return None
    m = pricing.modifier(speed, inference_geo) / 1_000_000
    return Rates(p.cache_read * m, p.output * m, p.input * pricing.WRITE_1H_MULT * m,
                 p.tier_threshold, p.tier_mult)


@dataclass(frozen=True)
class _Req:
    message_id: str
    stream: tuple[str, str | None]
    ts: float
    model: str
    context: int
    rates: Rates


@dataclass(frozen=True)
class CompactAnalysis:
    models: tuple[ModelThreshold, ...]  # models with at least one observed compaction
    global_: ModelThreshold | None  # None when there are no compactions at all
    own_costs: Mapping[str, tuple[float, float | None]] = field(default_factory=dict)
    # model -> (compaction cost, payback) at the global threshold and p75, at this model's prices

    # The autocompact rule, exposed for the CLI and the page to quote.
    CLAUDE_CODE_VERSION: ClassVar[str] = CLAUDE_CODE_VERSION
    AUTOCOMPACT_OUTPUT_RESERVE: ClassVar[int] = AUTOCOMPACT_OUTPUT_RESERVE
    AUTOCOMPACT_BUFFER: ClassVar[int] = AUTOCOMPACT_BUFFER
    AUTOCOMPACT_OFFSET: ClassVar[int] = AUTOCOMPACT_OFFSET
    MIN_AUTOCOMPACT_WINDOW: ClassVar[int] = MIN_AUTOCOMPACT_WINDOW
    MAX_AUTOCOMPACT_WINDOW: ClassVar[int] = MAX_AUTOCOMPACT_WINDOW

    def model_settings_json(self) -> str:
        """Copy-paste Claude Code `modelSettings`: every model's recommended window by model id."""
        return json.dumps({"modelSettings": {
            m.model: {"autoCompactWindow": m.autocompact_window} for m in self.models}}, indent=2)

    def for_model(self, model: str) -> ModelThreshold | None:
        for m in self.models:
            if m.model == model:
                return m
        g = self.global_
        if g is not None and model in self.own_costs:  # global T and p75, this model's prices
            cost, payback = self.own_costs[model]
            return replace(g, compaction_cost=cost, payback_requests=payback)
        return g


def p75(values: list[int]) -> int:
    s = sorted(values)
    return s[math.ceil(0.75 * len(s)) - 1]


def replay(reqs: list[_Req], threshold: int, post: int) -> float:
    """Total cost of `reqs` (oldest first) if each stream compacted whenever above `threshold`."""
    offset: dict[tuple, int] = {}
    last: dict[tuple, int] = {}
    total = 0.0
    for r in reqs:
        if r.context < last.get(r.stream, 0):  # a real compaction happened: the hypothetical
            offset[r.stream] = 0  # reduction no longer applies to the fresh context
        last[r.stream] = r.context
        ctx = max(0, r.context - offset.get(r.stream, 0))
        if ctx > threshold:
            offset[r.stream] = offset.get(r.stream, 0) + ctx - post
            total += r.rates.compaction_cost(ctx, post)
            ctx = post
        total += r.rates.read_cost(ctx)  # at the tier of the context left, not the observed one
    return total


def _fit(model: str | None, reqs: list[_Req], posts: list[int], source: str,
         step: int = STEP) -> ModelThreshold:
    post = p75(posts)
    actual = sum(r.rates.read_cost(r.context) for r in reqs)
    top = max((r.context for r in reqs), default=0)
    lo = (post // step + 1) * step  # a threshold must be above what a compaction leaves
    hi = max(lo, math.ceil(top / step) * step + step)
    curve = tuple(CurvePoint(t, replay(reqs, t, post)) for t in range(lo, hi + 1, step))
    best = min(curve, key=lambda c: (c.cost, -c.threshold))  # tie: do not compact earlier
    cost, payback = _cost_and_payback(reqs, post, best.threshold)
    return ModelThreshold(model, best.threshold, source, len(posts), post, cost, payback,
                          actual, curve)


def _cost_and_payback(reqs: list[_Req], post: int, threshold: int) -> tuple[float, float | None]:
    """Compaction cost and payback at `threshold`, averaged over the prices of `reqs`: compacting
    `threshold` pays its Prompt tier, and each later request reads `post` instead of `threshold`."""
    if not reqs:
        return 0.0, None
    cost = sum(r.rates.compaction_cost(threshold, post) for r in reqs) / len(reqs)
    saved_per_request = sum(r.rates.read_cost(threshold) - r.rates.read_cost(post)
                            for r in reqs) / len(reqs)
    return cost, cost / saved_per_request if saved_per_request > 0 else None


def analyze(conn: sqlite3.Connection, table=None, min_sample: int = MIN_SAMPLE,
            step: int = STEP) -> CompactAnalysis:
    rows = conn.execute(
        f"SELECT r.message_id, {SESSION_ID_SQL} AS sid, a.agent_id, "
        f"r.ts_epoch, r.model, {CONTEXT_SQL} AS ctx, ra.speed, ra.inference_geo FROM requests r "
        f"{SESSION_JOIN} "
        "LEFT JOIN request_surface a ON a.message_id = r.message_id "
        "LEFT JOIN request_attrs ra ON ra.message_id = r.message_id "
        "ORDER BY r.ts_epoch, r.message_id").fetchall()
    reqs: list[_Req] = []
    last_model: dict[str, list[tuple[float, str]]] = {}
    found: dict[tuple, Rates | None] = {}  # a handful of distinct prices, shared by every request
    for x in rows:
        k = (x["model"], price_day(x["ts_epoch"]), x["speed"], x["inference_geo"])
        if k not in found:
            found[k] = rates(*k, table)
        rs = found[k]
        if rs is None:
            continue
        reqs.append(_Req(x["message_id"], (x["sid"], x["agent_id"]), x["ts_epoch"], x["model"],
                         x["ctx"], rs))
        if x["agent_id"] is None:
            last_model.setdefault(x["sid"], []).append((x["ts_epoch"], x["model"]))

    posts: dict[str, list[int]] = {}
    for c in conn.execute(
            "SELECT c.ts_epoch, c.post_tokens, COALESCE(l.logical_session_id, c.stem) AS sid "
            "FROM compactions c LEFT JOIN session_links l ON l.stem = c.stem ORDER BY c.ts_epoch"):
        before = [m for t, m in last_model.get(c["sid"], []) if t <= c["ts_epoch"]]
        if before:  # a compaction belongs to the model of the request just before it
            posts.setdefault(before[-1], []).append(c["post_tokens"])
    all_posts = [p for ps in posts.values() for p in ps]
    if not all_posts:
        return CompactAnalysis((), None)

    global_ = _fit(None, reqs, all_posts, "global", step)
    by_model: dict[str, list[_Req]] = {}
    for r in reqs:
        by_model.setdefault(r.model, []).append(r)
    own_costs = {model: _cost_and_payback(mine, global_.post_tokens_p75, global_.threshold)
                 for model, mine in by_model.items()}
    models = []
    for model, ps in sorted(posts.items()):
        if len(ps) >= min_sample:
            models.append(_fit(model, [r for r in reqs if r.model == model], ps, "model", step))
        else:
            mine = [r for r in reqs if r.model == model]
            own = _fit(model, mine, ps, "model", step)
            cost, payback = _cost_and_payback(mine, own.post_tokens_p75, global_.threshold)
            models.append(ModelThreshold(
                model, global_.threshold, "global", len(ps), own.post_tokens_p75,
                cost, payback, own.actual_cost, global_.curve))
    return CompactAnalysis(tuple(models), global_, own_costs)


def key(table, min_sample: int, step: int) -> str:
    """What a stored analysis depends on: prices, settings and the stored shape. (The time zone
    is not an input: `price_day` is always the UTC day.)"""
    return json.dumps([pricing.fingerprint(table), min_sample, step, FORMAT])


_KEY_META, _ANALYSIS_META = "compact_key", "compact_analysis"  # the `meta` rows that hold it


def stored(conn: sqlite3.Connection, key: str) -> CompactAnalysis | None:
    """The analysis kept in `meta` if it was made for `key`; None when absent or made for other inputs."""
    rows = {r[0]: r[1] for r in conn.execute(
        "SELECT key, value FROM meta WHERE key IN (?, ?)", (_KEY_META, _ANALYSIS_META))}
    if rows.get(_KEY_META) != key or _ANALYSIS_META not in rows:
        return None
    return from_json(rows[_ANALYSIS_META])


def store(conn: sqlite3.Connection, key: str, analysis: CompactAnalysis) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (_ANALYSIS_META, to_json(analysis)))
        conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (_KEY_META, key))


def to_json(a: CompactAnalysis) -> str:
    return json.dumps({"models": [asdict(m) for m in a.models],
                       "global": None if a.global_ is None else asdict(a.global_),
                       "own_costs": a.own_costs})


def from_json(text: str) -> CompactAnalysis:
    def threshold(d: dict) -> ModelThreshold:
        return ModelThreshold(**{**d, "curve": tuple(CurvePoint(**c) for c in d["curve"])})
    d = json.loads(text)
    return CompactAnalysis(
        tuple(threshold(m) for m in d["models"]),
        None if d["global"] is None else threshold(d["global"]),
        {m: tuple(p) for m, p in d["own_costs"].items()})
