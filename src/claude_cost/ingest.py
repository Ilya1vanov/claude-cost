"""Ingest: reads main transcripts of each Data root into the store.

Skeleton behaviour: every run rescans all files and upserts by message id (so it is
idempotent). Incremental ingest by file size/offset is a later ticket.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import ownership, pricing, sources


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class Parsed:
    reqs: dict[str, dict] = field(default_factory=dict)
    relocated: bool = False
    links: set[str] = field(default_factory=set)  # other session ids of the same conversation


_SURFACES = {"cli": "CLI", "claude-desktop": "Desktop", "sdk-cli": "SDK"}


def surface_of(entrypoint: object) -> str:
    """Map a record's `entrypoint` to a Surface; anything unrecognised is "Other"."""
    if isinstance(entrypoint, str):
        if entrypoint in _SURFACES:
            return _SURFACES[entrypoint]
        if entrypoint.startswith("sdk"):
            return "SDK"
    return "Other"


def _parse_transcript(path: Path, stem: str | None = None) -> Parsed:
    """Group assistant lines by message id: first line wins, output = max over lines.

    `stem` is the Session id the transcript belongs to (a Subagent transcript belongs to
    its parent Session); it defaults to the filename stem.
    """
    stem = stem or path.stem
    parsed = Parsed()
    reqs = parsed.reqs
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # corrupt or partially written line
            if not isinstance(rec, dict):
                continue
            kind = rec.get("type")
            if kind == "relocated":
                parsed.relocated = True
                continue
            if kind == "continued-in":
                nxt = rec.get("continuedInSessionId")
                if isinstance(nxt, str) and nxt:
                    parsed.links.add(nxt)
                continue
            if kind != "assistant":
                continue
            msg = rec.get("message") or {}
            usage = msg.get("usage")
            model = msg.get("model")
            mid = msg.get("id")
            if not usage or not mid or not model or model == "<synthetic>":
                continue
            out = usage.get("output_tokens") or 0
            if mid in reqs:
                reqs[mid]["output"] = max(reqs[mid]["output"], out)
                continue
            fork = rec.get("forkedFrom")
            fork_sid = fork.get("sessionId") if isinstance(fork, dict) else None
            sid = fork_sid or rec.get("sessionId") or stem
            if not fork_sid and sid != stem:
                parsed.links.add(sid)  # resume or /clear copy: same conversation
            total_w = usage.get("cache_creation_input_tokens") or 0
            cc = usage.get("cache_creation") or {}
            w1h = cc.get("ephemeral_1h_input_tokens") or 0
            w5m = cc.get("ephemeral_5m_input_tokens") or 0
            if w5m + w1h < total_w:
                w5m = total_w - w1h
            reqs[mid] = {
                "session_id": sid,
                "is_copy": bool(fork_sid),
                "surface": surface_of(rec.get("entrypoint")),
                "ts": _parse_ts(rec["timestamp"]),
                "model": model,
                "input": usage.get("input_tokens") or 0,
                "output": out,
                "cache_read": usage.get("cache_read_input_tokens") or 0,
                "write_5m": w5m,
                "write_1h": w1h,
            }
    return parsed


def _group_sessions(first_ts: dict[str, datetime], links: set[tuple[str, str]]) -> dict[str, str]:
    """Union transcripts linked as one conversation; label each group by its earliest transcript."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for stem in first_ts:
        find(stem)
    for a, b in links:
        parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for stem in first_ts:
        groups.setdefault(find(stem), []).append(stem)
    return {stem: min(members, key=lambda s: (first_ts[s], s))
            for members in groups.values() for stem in members}


def _desktop_links(stores: list[Path], known: set[str]):
    """Read Desktop session stores: returns (stem pairs to group, {child stem: (parent stem, kind)}).

    Only transcripts that exist in `known` are linked. `priorCliSessionIds` are earlier
    transcripts of the same Desktop session; Side sessions (`spawnedFrom`) and Forks
    (`forkedFromSessionId`) name a parent Desktop session, resolved via its CLI session ids.
    """
    sessions: dict[str, list[str]] = {}  # desktop id -> its CLI session ids, current first
    meta: dict[str, dict] = {}
    for store in stores:
        for rec in sources.desktop_sessions(store):
            ids = [rec["cliSessionId"]] if isinstance(rec.get("cliSessionId"), str) else []
            prior = rec.get("priorCliSessionIds")
            ids += [i for i in prior if isinstance(i, str)] if isinstance(prior, list) else []
            sessions[rec["sessionId"]] = ids
            meta[rec["sessionId"]] = rec
    group = {(ids[0], other) for ids in sessions.values() for other in ids[1:]}

    def resolve(desktop_id: object) -> str | None:
        return next((i for i in sessions.get(desktop_id, []) if i in known), None)  # type: ignore[arg-type]

    relations: dict[str, tuple[str, str]] = {}
    for did, rec in meta.items():
        spawned = rec.get("spawnedFrom")
        if isinstance(spawned, dict):
            parent, kind = spawned.get("sessionId"), "side"
        else:
            parent, kind = rec.get("forkedFromSessionId"), "fork"
        child_stem, parent_stem = resolve(did), resolve(parent)
        if child_stem and parent_stem and child_stem != parent_stem:
            relations[child_stem] = (parent_stem, kind)
    return group, relations


def ingest(conn: sqlite3.Connection, roots: list[Path],
           desktop_stores: list[Path] | None = None) -> int:
    """Ingest all Data roots; returns the number of Requests in the store afterwards.

    `desktop_stores` are Desktop session store directories (scanned recursively) used to
    link Side sessions, Forks and earlier transcripts of the same Desktop session.
    """
    candidates: dict[str, list[dict]] = {}
    stem_first_ts: dict[str, datetime] = {}
    links: set[tuple[str, str]] = set()
    subagents: dict[tuple[str, str], dict] = {}
    for root in roots:
        for path in sources.main_transcripts(root):
            parsed = _parse_transcript(path)
            links.update((path.stem, other) for other in parsed.links)
            if not parsed.reqs:
                continue
            first_ts = min(r["ts"] for r in parsed.reqs.values())
            stem_first_ts[path.stem] = min(first_ts, stem_first_ts.get(path.stem, first_ts))
            for mid, r in parsed.reqs.items():
                candidates.setdefault(mid, []).append({
                    "file_stem": path.stem, "session_id": r["session_id"],
                    "file_first_ts": first_ts, "root": str(root),
                    "project": path.parent.name, "req": r,
                    "is_copy": r["is_copy"], "relocated": parsed.relocated,
                })
        for sub in sources.subagent_transcripts(root):
            parsed = _parse_transcript(sub.path, stem=sub.session_stem)
            if not parsed.reqs:
                continue
            links.update((sub.session_stem, other) for other in parsed.links)
            first_ts = min(r["ts"] for r in parsed.reqs.values())
            stem_first_ts.setdefault(sub.session_stem, first_ts)  # parent transcript may be gone
            stem_first_ts[sub.session_stem] = min(first_ts, stem_first_ts[sub.session_stem])
            meta = sub.meta
            subagents[(sub.session_stem, sub.agent_id)] = {
                "type": meta.get("agentType") if isinstance(meta.get("agentType"), str) else None,
                "is_fork": 1 if meta.get("isFork") is True or meta.get("agentType") == "fork" else 0,
                "tool_use_id": meta.get("toolUseId") if isinstance(meta.get("toolUseId"), str) else None,
                "parent_agent_id": meta.get("parentAgentId") if isinstance(meta.get("parentAgentId"), str) else None,
                "description": meta.get("description") if isinstance(meta.get("description"), str) else None,
            }
            for mid, r in parsed.reqs.items():
                candidates.setdefault(mid, []).append({
                    "file_stem": sub.session_stem, "session_id": r["session_id"],
                    "file_first_ts": first_ts, "root": str(root),
                    "project": sub.project, "req": r, "agent_id": sub.agent_id,
                    "is_copy": r["is_copy"], "relocated": False, "is_subagent": True,
                })
    group_pairs, relations = _desktop_links(desktop_stores or [], set(stem_first_ts))
    links |= group_pairs
    with conn:
        conn.execute("DELETE FROM subagents")
        conn.execute("DELETE FROM session_relations")
        conn.executemany(
            "INSERT INTO subagents VALUES (?,?,?,?,?,?,?)",
            [(sid, aid, m["type"], m["is_fork"], m["tool_use_id"], m["parent_agent_id"],
              m["description"]) for (sid, aid), m in subagents.items()])
        conn.executemany(
            "INSERT INTO session_relations VALUES (?,?,?)",
            [(child, parent, kind) for child, (parent, kind) in relations.items()])
        conn.execute("DELETE FROM session_links")
        conn.executemany(
            "INSERT INTO session_links VALUES (?,?)",
            _group_sessions(stem_first_ts, links).items())
        for mid, cands in candidates.items():
            owner = ownership.pick_owner(cands)
            r = owner["req"]
            cost = pricing.price_request(
                r["model"], r["ts"].date(), input=r["input"], output=r["output"],
                cache_read=r["cache_read"], write_5m=r["write_5m"], write_1h=r["write_1h"],
            )
            c = cost or pricing.CostComponents()
            conn.execute(
                "INSERT OR REPLACE INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, owner["root"], owner["project"], owner["file_stem"],
                 r["ts"].timestamp(), r["model"], r["input"], r["output"], r["cache_read"],
                 r["write_5m"], r["write_1h"], 1 if cost else 0,
                 c.input, c.output, c.cache_write_5m, c.cache_write_1h, c.cache_read),
            )
            conn.execute("INSERT OR REPLACE INTO request_attrs VALUES (?,?,?)",
                         (mid, r["surface"], owner.get("agent_id")))
    return conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
