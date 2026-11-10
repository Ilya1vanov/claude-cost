"""Drain: Unused listing. A skill or MCP server Listing loaded into a project's sessions
that was never invoked in that project over the period. Avoidable is a range: the lower bound is
its read cost; the upper bound adds the write that stored it, the Cold writes that rebuilt it in
place of a read, and any Prompt tier drop its removal would cause (see `Stream.extra`)."""
from __future__ import annotations

import json
from collections import defaultdict
from functools import lru_cache

from ..projects import project_display_names
from .block_pricing import Stream
from .core import (DRAIN, CollapsibleCell, Detection, DetailTable, FindingContext, FindingDef,
                   format_avoidable, ts_where)

TOP = 25  # table rows before the rest are summed into one


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


@lru_cache(maxsize=4096)  # a Listing name is split once, however many times it is matched
def _suffixes(name: str) -> frozenset[str]:
    """What follows each ':' in `name`: a skill may be invoked without its plugin prefix, and
    the prefix itself may contain ':'."""
    return frozenset(name[i + 1:] for i, c in enumerate(name) if c == ":")


def _used(listing_name: str, kind: str, used: set[str]) -> bool:
    return listing_name in used or (kind == "skill" and not used.isdisjoint(_suffixes(listing_name)))


def detect(ctx: FindingContext) -> Detection:
    period = ctx.rows
    if ctx.conn is None or not period:
        return Detection([])
    by_stream: dict[tuple[str, str], list] = defaultdict(list)  # (session, agent) -> requests
    for r in period:
        by_stream[(r.session_id, r.agent_id or "")].append(r)
    # Listings and removals are read for the active Streams only, selected by Stream and never
    # by time: a Stream's earliest Listing and its removals can predate the Period.
    stems = json.dumps(sorted({t for sid, _ in by_stream for t in ctx.graph.transcripts(sid)}))
    # a use is placed by its own timestamp; a side with no bound is all history
    names_used: dict[tuple[str, str], set[str]] = defaultdict(set)  # (project, kind) -> names
    uses: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))  # kind -> name -> uses
    where, args = ts_where("ts_epoch", ctx.period_start, ctx.period_end)
    for x in ctx.conn.execute("SELECT project, kind, name, COUNT(*) AS n FROM tool_uses "
                              f"{where}GROUP BY project, kind, name", args):
        names_used[(x["project"], x["kind"])].add(x["name"])
        uses[x["kind"]][x["name"]] += x["n"]
    where, args = ts_where("r.ts_epoch", ctx.period_start, ctx.period_end)
    for x in ctx.conn.execute("SELECT r.project, s.skill, COUNT(*) AS n FROM request_skills s "
                              "JOIN requests r ON r.message_id = s.message_id "
                              f"{where}GROUP BY r.project, s.skill", args):
        # slash commands and attributionSkill count as uses too
        names_used[(x["project"], "skill")].add(x["skill"])
        uses["slash"][x["skill"]] += x["n"]
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
                   for m in (name, *(_suffixes(name) if kind == "skill" else ())))
    streams: dict[tuple[str, str], Stream] = {}
    items: dict[tuple, dict] = {}
    for (project, sid, agent, kind, name), (ts, tokens) in listed.items():
        reqs = by_stream.get((sid, agent))
        if not reqs:
            continue  # session not active in the period
        if _used(name, kind, names_used[(project, kind)]):
            continue
        stream = streams.get((sid, agent))
        if stream is None:  # period rows are oldest first, so each stream's requests already are
            stream = streams[(sid, agent)] = Stream(ctx, reqs)
        # a removed Listing is no longer read from that point
        gone = min((t for t in removed.get((sid, agent, kind, name), ()) if t > ts), default=None)
        charged = stream.charge(ts, gone, tokens, (project, kind, name))
        if charged is None:
            continue  # never read again, so it cost nothing
        cost, high = charged
        it = items.setdefault((project, kind, name), {"sessions": set(), "cost": 0.0, "high": 0.0})
        it["sessions"].add(sid)
        it["cost"] += cost
        it["high"] += high
    for (sid, _), st in streams.items():
        for key, dollars in st.split_tier_drops():  # a Listing never read has only its writer's drop
            it = items.setdefault(key, {"sessions": set(), "cost": 0.0, "high": 0.0})
            it["sessions"].add(sid)
            it["high"] += dollars
    covered = [c for st in streams.values() for c in st.covered()]
    if not items:
        return Detection(covered)
    names = project_display_names(ctx.conn)  # needs every project: a name is told apart from its namesakes
    by_listing: dict[tuple[str, str], list] = defaultdict(list)  # (kind, name) -> [(project, item)]
    for (project, kind, name), i in items.items():
        by_listing[(kind, name)].append((names.get(project, project), i))
    ranked = sorted(by_listing.items(), key=lambda kv: (-sum(i["cost"] for _, i in kv[1]), kv[0]))
    rows = []
    for (kind, name), per_project in ranked[:TOP]:
        n = len(per_project)
        cells = tuple(f"{p}: {format_avoidable(i['cost'], i['high'])}, {len(i['sessions'])} session"
                      f"{'' if len(i['sessions']) == 1 else 's'}"
                      for p, i in sorted(per_project, key=lambda x: (-x[1]["cost"], x[0])))
        rows.append((name, kind, CollapsibleCell(f"{n} project{'' if n == 1 else 's'}", cells),
                     str(elsewhere(kind, name)), format_avoidable(sum(i['cost'] for _, i in per_project),
                                                              sum(i['high'] for _, i in per_project))))
    if rest := ranked[TOP:]:
        rows.append((_count(len(rest), "more listing"), "", "", "",
                     format_avoidable(sum(i['cost'] for _, pp in rest for _, i in pp),
                                      sum(i['high'] for _, pp in rest for _, i in pp))))
    projects = {p for p, _, _ in items}
    table = DetailTable(("Listing", "Kind", "Unused in", "Used elsewhere", "Avoidable"), tuple(rows))
    return Detection(covered, f"{_count(len(by_listing), 'unused listing')} in "
                              f"{_count(len(projects), 'project')}", table)


FINDING = FindingDef(
    id="unused-listing", kind=DRAIN, title="Unused listing",
    description="Skills and MCP servers listed in a project's sessions but never invoked in "
                "that project over the period.",
    formula="avoidable = lower bound: Listing tokens (text length / 3.6) x cache read price, for "
            "every request after the one that wrote the Listing; upper bound: the lower bound "
            "plus the Listing's tokens at write price on the Period request that wrote it, and on each "
            "Cold-write request in place of its read (never more tokens than the request wrote), plus "
            "(tier multiplier - 1) / tier multiplier of a request's whole cost when removing all its "
            "unused Listings takes its prompt from above its model's Prompt tier threshold to at or "
            "below it (split across those Listings by token share)",
    detect=detect)
