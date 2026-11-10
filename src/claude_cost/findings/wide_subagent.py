"""Opportunity: Wide subagent. Subagents whose first request has a large context. Avoidable is a
range from $0 (delegating wide may be intentional) to an upper bound: the cost of the subagent's
extra context, its first-request context minus the Bare subagent context, priced as one block
from its first request to its Stream's end or first compaction (see `Stream`). Forks stay covered
with no figure."""
from __future__ import annotations

import json
from collections import defaultdict

from ..cache import BARE_MIN_RUNS
from .block_pricing import Stream
from .core import OPPORTUNITY, Covered, Detection, FindingContext, FindingDef, Threshold

THRESHOLD = "wide_subagent_context_tokens"
BARE_THRESHOLD = "bare_subagent_context_tokens"
BARE_FALLBACK = 5_000  # tokens, when no subagent type has BARE_MIN_RUNS non-fork runs


def _thousands(tokens: float) -> str:
    return f"{tokens / 1000:.1f}K"


def bare_context(ctx: FindingContext) -> tuple[float, str]:
    """(tokens, source text): the configured value, else the learned one, else the fallback."""
    if configured := ctx.threshold(BARE_THRESHOLD):
        return configured, "config"
    learned = ctx.bare_subagent
    if learned is not None:
        return learned.tokens, f"{learned.agent_type} median, {learned.runs} runs"
    return BARE_FALLBACK, "fallback"


def _first_compactions(ctx: FindingContext, streams) -> dict[tuple[str, str], float]:
    """(session, agent) -> the Stream's first compaction, for the given (session, agent) keys."""
    if ctx.conn is None or not streams:
        return {}
    stems = {t for sid, _ in streams for t in ctx.graph.transcripts(sid)}
    first: dict[tuple[str, str], float] = {}
    for x in ctx.conn.execute(
            "SELECT stem, agent_id, MIN(ts_epoch) AS ts FROM subagent_compactions "
            "WHERE stem IN (SELECT value FROM json_each(?)) GROUP BY stem, agent_id",
            (json.dumps(sorted(stems)),)):
        key = (ctx.graph.session_of(x["stem"]), x["agent_id"])
        if key in streams:
            first[key] = min(x["ts"], first.get(key, x["ts"]))
    return first


def detect(ctx: FindingContext) -> Detection:
    limit = ctx.threshold(THRESHOLD)
    bare, source = bare_context(ctx)
    wide = [r for r in ctx.rows if r.agent_id and r.stream_first_context >= limit]
    by_stream: dict[tuple[str, str], list] = defaultdict(list)  # (session, agent) -> requests, oldest first
    for r in wide:
        if not r.agent_fork:
            by_stream[(r.session_id, r.agent_id)].append(r)
    compacted = _first_compactions(ctx, set(by_stream))
    figures: dict[str, Covered] = {}
    streams = []
    for key, reqs in by_stream.items():
        extra = max(0, round(reqs[0].stream_first_context - bare))
        stream = Stream(ctx, reqs)
        streams.append(stream)
        # a Stream whose first request precedes the Period was written before it: no write to charge
        start = reqs[0].ts if stream.prev_ts is None else float("-inf")
        if extra:
            stream.charge(start, compacted.get(key), extra, own_writer=True)
    for stream in streams:
        stream.split_tier_drops()
        figures.update((c.message_id, c) for c in stream.covered())
    covered = []
    for r in wide:
        if r.agent_fork:
            covered.append(Covered(r.message_id))
        else:
            c = figures.get(r.message_id)  # $0 lower bound: nothing is claimed for sure
            covered.append(Covered(r.message_id, avoidable=0.0, avoidable_high=c.avoidable_high if c else 0.0))
    return Detection(
        covered, f"first-request context >= {limit:,} tokens",
        formula=f"avoidable = $0 to an upper bound: the extra context (first-request context - bare "
                f"context {_thousands(bare)} ({source})) as one block from the first request to the end or "
                f"first compaction: its write on the first request (if in the Period), its reads on "
                f"every later request, each Cold write that rebuilt it (write price - read price) "
                f"and any Prompt tier drop its removal would cause")


FINDING = FindingDef(
    id="wide-subagent", kind=OPPORTUNITY, title="Wide subagent",
    description="Subagents whose first request already carries a large context. "
                "May be intentional; shown for review.",
    formula="avoidable = $0 to an upper bound: the cost of the extra context (first-request "
            "context - Bare subagent context) as one block",
    detect=detect,
    thresholds=(
        Threshold(THRESHOLD, 50_000, "Minimum first-request context (tokens) of a Wide subagent",
                  minimum=1),
        Threshold(BARE_THRESHOLD, 0, "Bare subagent context (tokens) Wide subagent compares against; "
                  f"0 = learn it from your history (leanest subagent type with {BARE_MIN_RUNS}+ "
                  f"runs, else {BARE_FALLBACK:,}).", minimum=0),
    ))
