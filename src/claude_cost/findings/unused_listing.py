"""Drain: Unused listing. A skill or MCP server Listing loaded into a project's sessions
that was never invoked in that project over the period. Avoidable = its full read cost."""
from __future__ import annotations

from collections import defaultdict

from .. import pricing
from ..projects import project_display_names
from .core import DRAIN, Covered, Detection, FindingContext, FindingDef


def _used(listing_name: str, kind: str, used: set[str]) -> bool:
    if listing_name in used:
        return True
    # a skill may be invoked without its plugin prefix
    return kind == "skill" and any(listing_name.endswith(f":{u}") for u in used)


def _read_price(ctx: FindingContext, r, tokens: int) -> float:
    c = pricing.price_request(r.model, r.day, input=0, output=0, cache_read=tokens, write_5m=0,
                              write_1h=0, speed=r.speed, inference_geo=r.inference_geo,
                              table=ctx.price_table)
    return c.cache_read if c else 0.0


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
    elsewhere: dict[str, int] = defaultdict(int)  # kind:name -> invocations in any project
    for proj, counts in used.items():
        for k, n in counts.items():
            elsewhere[k] += n
    avoidable: dict[str, float] = defaultdict(float)
    items: dict[tuple, dict] = {}
    for (project, sid, agent, kind, name), (ts, tokens) in listed.items():
        reqs = by_stream.get((sid, agent))
        if not reqs:
            continue  # session not active in the period
        u = {k.split(":", 1)[1] for k in used[project] if k.startswith(kind + ":")}
        if kind == "skill":
            u |= attributed[project]
        if _used(name, kind, u):
            continue
        # requests after the one that wrote the Listing read it from the cache
        later = [r for r in sorted(reqs, key=lambda r: (r.ts, r.message_id)) if r.ts >= ts][1:]
        gone = min((t for t in removed.get((sid, agent, kind, name), ()) if t > ts), default=None)
        if gone is not None:  # a removed Listing is no longer read from that point
            later = [r for r in later if r.ts < gone]
        cost = 0.0
        for r in later:
            c = _read_price(ctx, r, tokens)
            avoidable[r.message_id] += c
            cost += c
        it = items.setdefault((project, kind, name), {"sessions": set(), "cost": 0.0,
                                                      "elsewhere": elsewhere.get(f"{kind}:{name}", 0)})
        it["sessions"].add(sid)
        it["cost"] += cost
    covered = [Covered(mid, a) for mid, a in avoidable.items()]
    names = project_display_names(ctx.conn)
    parts = [f"{kind} {name} in {names.get(project, project)}: {len(i['sessions'])} sessions listed, "
             f"0 invocations here ({i['elsewhere']} in other projects), ${i['cost']:.2f}"
             for (project, kind, name), i in sorted(items.items(), key=lambda kv: -kv[1]["cost"])]
    return Detection(covered, "; ".join(parts))


FINDING = FindingDef(
    id="unused-listing", kind=DRAIN, title="Unused listing",
    description="Skills and MCP servers listed in a project's sessions but never invoked in "
                "that project over the period.",
    formula="avoidable = Listing tokens (text length / 3.6) x cache read price, for every "
            "request after the one that wrote the Listing",
    detect=detect)
