"""Drain: Unused listing. A skill or MCP server Listing loaded into a project's sessions
that was never invoked in that project over the period. Avoidable = its full read cost."""
from __future__ import annotations

import json
from bisect import bisect_left
from collections import defaultdict
from itertools import accumulate

from .. import pricing
from ..projects import project_display_names
from .core import DRAIN, CollapsibleCell, Covered, Detection, DetailTable, FindingContext, FindingDef

TOP = 25  # table rows before the rest are summed into one


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _suffixes(name: str) -> frozenset[str]:
    """What follows each ':' in `name`: a skill may be invoked without its plugin prefix, and
    the prefix itself may contain ':'."""
    return frozenset(name[i + 1:] for i, c in enumerate(name) if c == ":")


def _used(listing_name: str, kind: str, used: set[str], suffixes=_suffixes) -> bool:
    return listing_name in used or (kind == "skill" and not used.isdisjoint(suffixes(listing_name)))


def _read_rate(ctx: FindingContext, r) -> float:
    """Cache read $ per token for request `r`, below any prompt-size tier."""
    c = pricing.price_request(r.model, r.day, input=0, output=0, cache_read=1, write_5m=0,
                              write_1h=0, speed=r.speed, inference_geo=r.inference_geo,
                              table=ctx.price_table)
    return c.cache_read if c else 0.0


class _Stream:
    """One (session, agent) stream's period requests, oldest first, with each request's read
    rate and the unused Listing tokens it reads (kept as a difference array)."""

    def __init__(self, ctx: FindingContext, reqs: list):
        self.reqs = reqs
        self.ts = [r.ts for r in reqs]
        self.rates = [_read_rate(ctx, r) for r in reqs]
        self.delta = [0] * (len(reqs) + 1)

    def charge(self, ts: float, gone: float | None, tokens: int) -> float | None:
        """Charge a Listing written at `ts` (removed at `gone`) to the requests that read it from
        the cache: those after the one that wrote it, before the removal. Returns its $, or
        None when no request read it."""
        lo = bisect_left(self.ts, ts) + 1
        hi = len(self.reqs) if gone is None else bisect_left(self.ts, gone)
        if lo >= hi:
            return None
        self.delta[lo] += tokens
        self.delta[hi] -= tokens
        # summed, not prefix-subtracted, so equal reads give equal $ and ties sort by name
        return tokens * sum(self.rates[lo:hi])

    def covered(self) -> list[Covered]:
        return [Covered(r.message_id, tokens * rate)
                for r, rate, tokens in zip(self.reqs, self.rates, accumulate(self.delta)) if tokens]


def detect(ctx: FindingContext) -> Detection:
    period = ctx.rows
    if ctx.conn is None or not period:
        return Detection([])
    start = min(r.ts for r in period)
    by_stream: dict[tuple[str, str], list] = defaultdict(list)  # (session, agent) -> requests
    for r in period:
        by_stream[(r.session_id, r.agent_id or "")].append(r)
    suffix_memo: dict[str, frozenset[str]] = {}  # per run: each Listing name is split once

    def suffixes(name: str) -> frozenset[str]:
        if name not in suffix_memo:
            suffix_memo[name] = _suffixes(name)
        return suffix_memo[name]

    # Listings and removals are read for the active Streams only, selected by Stream and never
    # by time: a Stream's earliest Listing and its removals can predate the Period.
    stems = json.dumps(sorted({t for sid, _ in by_stream for t in ctx.graph.transcripts(sid)}))
    names_used: dict[tuple[str, str], set[str]] = defaultdict(set)  # (project, kind) -> names
    uses: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))  # kind -> name -> uses
    for x in ctx.conn.execute("SELECT project, kind, name FROM tool_uses WHERE ts_epoch >= ?",
                              (start,)):
        names_used[(x["project"], x["kind"])].add(x["name"])
        uses[x["kind"]][x["name"]] += 1
    for x in ctx.conn.execute("SELECT r.project, s.skill FROM request_skills s "
                              "JOIN requests r ON r.message_id = s.message_id "
                              "WHERE r.ts_epoch >= ?", (start,)):
        # slash commands and attributionSkill count as uses too
        names_used[(x["project"], "skill")].add(x["skill"])
        uses["slash"][x["skill"]] += 1
    # earliest Listing per (project, session, agent, kind, name); resume copies repeat them
    listed: dict[tuple, tuple[float, int]] = {}
    for x in ctx.conn.execute("SELECT stem, agent_id, project, kind, name, ts_epoch, tokens "
                              "FROM listings WHERE stem IN (SELECT value FROM json_each(?)) "
                              "ORDER BY ts_epoch", (stems,)):
        sid = ctx.graph.session_of(x["stem"])
        key = (x["project"], sid, x["agent_id"], x["kind"], x["name"])
        listed.setdefault(key, (x["ts_epoch"], x["tokens"]))
    removed: dict[tuple, list[float]] = defaultdict(list)  # removal times
    for x in ctx.conn.execute("SELECT stem, agent_id, kind, name, ts_epoch FROM listing_removals "
                              "WHERE stem IN (SELECT value FROM json_each(?))", (stems,)):
        sid = ctx.graph.session_of(x["stem"])
        removed[(sid, x["agent_id"], x["kind"], x["name"])].append(x["ts_epoch"])
    def elsewhere(kind: str, name: str) -> int:
        """Invocations of the listing, matched like `_used`, in every project."""
        pools = [uses[kind]] + ([uses["slash"]] if kind == "skill" else [])  # slash commands too
        return sum(pool.get(m, 0) for pool in pools
                   for m in (name, *(suffixes(name) if kind == "skill" else ())))
    streams: dict[tuple[str, str], _Stream] = {}
    items: dict[tuple, dict] = {}
    for (project, sid, agent, kind, name), (ts, tokens) in listed.items():
        reqs = by_stream.get((sid, agent))
        if not reqs:
            continue  # session not active in the period
        if _used(name, kind, names_used[(project, kind)], suffixes):
            continue
        stream = streams.get((sid, agent))
        if stream is None:  # period rows are oldest first, so each stream's requests already are
            stream = streams[(sid, agent)] = _Stream(ctx, reqs)
        # a removed Listing is no longer read from that point
        gone = min((t for t in removed.get((sid, agent, kind, name), ()) if t > ts), default=None)
        cost = stream.charge(ts, gone, tokens)
        if cost is None:
            continue  # never read again, so it cost nothing
        it = items.setdefault((project, kind, name), {"sessions": set(), "cost": 0.0})
        it["sessions"].add(sid)
        it["cost"] += cost
    covered = [c for st in streams.values() for c in st.covered()]
    names = project_display_names(ctx.conn)
    by_listing: dict[tuple[str, str], list] = defaultdict(list)  # (kind, name) -> [(project, item)]
    for (project, kind, name), i in items.items():
        by_listing[(kind, name)].append((names.get(project, project), i))
    ranked = sorted(by_listing.items(), key=lambda kv: (-sum(i["cost"] for _, i in kv[1]), kv[0]))
    rows = []
    for (kind, name), per_project in ranked[:TOP]:
        n = len(per_project)
        cells = tuple(f"{p}: ${i['cost']:.2f}, {len(i['sessions'])} session"
                      f"{'' if len(i['sessions']) == 1 else 's'}"
                      for p, i in sorted(per_project, key=lambda x: (-x[1]["cost"], x[0])))
        rows.append((name, kind, CollapsibleCell(f"{n} project{'' if n == 1 else 's'}", cells),
                     str(elsewhere(kind, name)), f"${sum(i['cost'] for _, i in per_project):.2f}"))
    if rest := ranked[TOP:]:
        rows.append((_count(len(rest), "more listing"), "", "", "",
                     f"${sum(i['cost'] for _, pp in rest for _, i in pp):.2f}"))
    if not rows:
        return Detection(covered)
    projects = {p for p, _, _ in items}
    table = DetailTable(("Listing", "Kind", "Unused in", "Used elsewhere", "Avoidable"), tuple(rows))
    return Detection(covered, f"{_count(len(by_listing), 'unused listing')} in "
                              f"{_count(len(projects), 'project')}", table)


FINDING = FindingDef(
    id="unused-listing", kind=DRAIN, title="Unused listing",
    description="Skills and MCP servers listed in a project's sessions but never invoked in "
                "that project over the period.",
    formula="avoidable = Listing tokens (text length / 3.6) x cache read price, for every "
            "request after the one that wrote the Listing",
    detect=detect)
