"""Drain: Unused listing. A skill or MCP server Listing loaded into a project's sessions
that was never invoked in that project over the period. Avoidable = its full read cost."""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from itertools import accumulate

from .. import pricing
from ..projects import project_display_names
from .core import DRAIN, CollapsibleCell, Covered, Detection, DetailTable, FindingContext, FindingDef


def _used(listing_name: str, kind: str, used: set[str]) -> bool:
    if listing_name in used:
        return True
    # a skill may be invoked without its plugin prefix
    return kind == "skill" and any(listing_name.endswith(f":{u}") for u in used)


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

    def charge(self, ts: float, gone: float | None, tokens: int) -> float:
        """Charge a Listing written at `ts` (removed at `gone`) to the requests that read it from
        the cache: those after the one that wrote it, before the removal. Returns its $."""
        lo = bisect_left(self.ts, ts) + 1
        hi = len(self.reqs) if gone is None else bisect_left(self.ts, gone)
        if lo >= hi:
            return 0.0
        self.delta[lo] += tokens
        self.delta[hi] -= tokens
        # summed, not prefix-subtracted, so equal reads give equal $ and ties sort by name
        return tokens * sum(self.rates[lo:hi])

    def covered(self) -> list[Covered]:
        return [Covered(r.message_id, tokens * rate)
                for r, rate, tokens in zip(self.reqs, self.rates, accumulate(self.delta)) if tokens]


def detect(ctx: FindingContext) -> Detection:
    period = ctx.period_rows
    if ctx.conn is None or not period:
        return Detection([])
    start = min(r.ts for r in period)
    sid_of = {x["stem"]: x["sid"] for x in ctx.conn.execute(
        "SELECT stem, logical_session_id AS sid FROM session_links")}
    agent_of = {x["message_id"]: x["agent_id"] or "" for x in ctx.conn.execute(
        "SELECT message_id, agent_id FROM request_surface")}
    by_stream: dict[tuple[str, str], list] = defaultdict(list)  # (session, agent) -> requests
    for r in period:
        by_stream[(r.session_id, agent_of.get(r.message_id, ""))].append(r)
    used: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for x in ctx.conn.execute("SELECT project, kind, name, ts_epoch FROM tool_uses"):
        if x["ts_epoch"] >= start:
            used[x["project"]][x["kind"] + ":" + x["name"]] += 1
    attributed: dict[str, set[str]] = defaultdict(set)  # project -> skills used by slash command
    for x in ctx.conn.execute("SELECT r.project, r.ts_epoch, s.skill FROM request_skills s "
                              "JOIN requests r ON r.message_id = s.message_id"):
        if x["ts_epoch"] >= start:  # slash commands and attributionSkill count as uses too
            attributed[x["project"]].add(x["skill"])
    # earliest Listing per (project, session, agent, kind, name); resume copies repeat them
    listed: dict[tuple, tuple[float, int]] = {}
    for x in ctx.conn.execute("SELECT stem, agent_id, project, kind, name, ts_epoch, tokens "
                              "FROM listings ORDER BY ts_epoch"):
        sid = sid_of.get(x["stem"], x["stem"])
        key = (x["project"], sid, x["agent_id"], x["kind"], x["name"])
        listed.setdefault(key, (x["ts_epoch"], x["tokens"]))
    removed: dict[tuple, list[float]] = defaultdict(list)  # removal times
    for x in ctx.conn.execute("SELECT stem, agent_id, kind, name, ts_epoch FROM listing_removals"):
        sid = sid_of.get(x["stem"], x["stem"])
        removed[(sid, x["agent_id"], x["kind"], x["name"])].append(x["ts_epoch"])
    def elsewhere(kind: str, name: str) -> int:
        """Invocations of the listing, matched like `_used`, in every project."""
        return sum(n for counts in used.values() for k, n in counts.items()
                   if k.startswith(kind + ":") and _used(name, kind, {k.split(":", 1)[1]}))
    used_names: dict[tuple[str, str], set[str]] = {}  # (project, kind) -> names used there
    streams: dict[tuple[str, str], _Stream] = {}
    items: dict[tuple, dict] = {}
    for (project, sid, agent, kind, name), (ts, tokens) in listed.items():
        reqs = by_stream.get((sid, agent))
        if not reqs:
            continue  # session not active in the period
        u = used_names.get((project, kind))
        if u is None:
            u = {k.split(":", 1)[1] for k in used[project] if k.startswith(kind + ":")}
            if kind == "skill":
                u |= attributed[project]
            used_names[(project, kind)] = u
        if _used(name, kind, u):
            continue
        stream = streams.get((sid, agent))
        if stream is None:  # period rows are oldest first, so each stream's requests already are
            stream = streams[(sid, agent)] = _Stream(ctx, reqs)
        # a removed Listing is no longer read from that point
        gone = min((t for t in removed.get((sid, agent, kind, name), ()) if t > ts), default=None)
        it = items.setdefault((project, kind, name), {"sessions": set(), "cost": 0.0})
        it["sessions"].add(sid)
        it["cost"] += stream.charge(ts, gone, tokens)
    covered = [c for st in streams.values() for c in st.covered()]
    names = project_display_names(ctx.conn)
    by_listing: dict[tuple[str, str], list] = defaultdict(list)  # (kind, name) -> [(project, item)]
    for (project, kind, name), i in items.items():
        by_listing[(kind, name)].append((names.get(project, project), i))
    rows = []
    for (kind, name), per_project in sorted(
            by_listing.items(), key=lambda kv: (-sum(i["cost"] for _, i in kv[1]), kv[0])):
        n = len(per_project)
        cells = tuple(f"{p}: ${i['cost']:.2f}, {len(i['sessions'])} session"
                      f"{'' if len(i['sessions']) == 1 else 's'}"
                      for p, i in sorted(per_project, key=lambda x: (-x[1]["cost"], x[0])))
        rows.append((name, kind, CollapsibleCell(f"{n} project{'' if n == 1 else 's'}", cells),
                     str(elsewhere(kind, name)), f"${sum(i['cost'] for _, i in per_project):.2f}"))
    table = DetailTable(("Listing", "Kind", "Unused in", "Used elsewhere", "Avoidable"), tuple(rows))
    return Detection(covered, table=table if rows else None)


FINDING = FindingDef(
    id="unused-listing", kind=DRAIN, title="Unused listing",
    description="Skills and MCP servers listed in a project's sessions but never invoked in "
                "that project over the period.",
    formula="avoidable = Listing tokens (text length / 3.6) x cache read price, for every "
            "request after the one that wrote the Listing",
    detect=detect)
