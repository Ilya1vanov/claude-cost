"""Ingest: reads main transcripts of each Data root into the store.

Skeleton behaviour: every run rescans all files and upserts by message id (so it is
idempotent). Incremental ingest by file size/offset is a later ticket.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import ownership, pricing, sources


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class Parsed:
    reqs: dict[str, dict] = field(default_factory=dict)
    relocated: bool = False
    links: set[str] = field(default_factory=set)  # other session ids of the same conversation


def _parse_transcript(path: Path) -> Parsed:
    """Group assistant lines by message id: first line wins, output = max over lines."""
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
            sid = fork_sid or rec.get("sessionId") or path.stem
            if not fork_sid and sid != path.stem:
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
                "ts": _parse_ts(rec["timestamp"]),
                "model": model,
                "input": usage.get("input_tokens") or 0,
                "output": out,
                "cache_read": usage.get("cache_read_input_tokens") or 0,
                "write_5m": w5m,
                "write_1h": w1h,
                "speed": usage.get("speed") or "standard",
                "inference_geo": usage.get("inference_geo") or "",
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


def reprice(conn: sqlite3.Connection, table: dict) -> None:
    """Re-price every stored Request when the price table in effect changed since last time."""
    fp = hashlib.sha256(repr(sorted(table.items())).encode()).hexdigest()
    row = conn.execute("SELECT fingerprint FROM pricing_state").fetchone()
    if row and row["fingerprint"] == fp:
        return
    updates = []
    rows = conn.execute(
        "SELECT r.*, a.speed, a.inference_geo FROM requests r "
        "LEFT JOIN request_attrs a ON a.message_id = r.message_id").fetchall()
    for r in rows:
        cost = pricing.price_request(
            r["model"], datetime.fromtimestamp(r["ts_epoch"], timezone.utc).date(),
            input=r["input_tokens"], output=r["output_tokens"], cache_read=r["cache_read_tokens"],
            write_5m=r["cache_write_5m_tokens"], write_1h=r["cache_write_1h_tokens"],
            speed=r["speed"], inference_geo=r["inference_geo"], table=table)
        c = cost or pricing.CostComponents()
        updates.append((1 if cost else 0, c.input, c.output, c.cache_write_5m,
                        c.cache_write_1h, c.cache_read, r["message_id"]))
    with conn:
        conn.executemany(
            "UPDATE requests SET priced=?, cost_input=?, cost_output=?, cost_cache_write_5m=?, "
            "cost_cache_write_1h=?, cost_cache_read=? WHERE message_id=?", updates)
        conn.execute("INSERT OR REPLACE INTO pricing_state VALUES (1, ?)", (fp,))


def ingest(conn: sqlite3.Connection, roots: list[Path], price_table: dict | None = None) -> int:
    """Ingest all Data roots; returns the number of Requests in the store afterwards."""
    table = pricing.PRICE_TABLE if price_table is None else price_table
    candidates: dict[str, list[dict]] = {}
    stem_first_ts: dict[str, datetime] = {}
    links: set[tuple[str, str]] = set()
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
    with conn:
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
                speed=r["speed"], inference_geo=r["inference_geo"], table=table,
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
                         (mid, r["speed"], r["inference_geo"]))
    reprice(conn, table)
    return conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
