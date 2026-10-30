"""Ingest: reads main transcripts of each Data root into the store.

Skeleton behaviour: every run rescans all files and upserts by message id (so it is
idempotent). Incremental ingest by file size/offset is a later ticket.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import ownership, pricing, sources


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _parse_transcript(path: Path) -> dict[str, dict]:
    """Group assistant lines by message id: first line wins, output = max over lines."""
    reqs: dict[str, dict] = {}
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # corrupt or partially written line
            if not isinstance(rec, dict) or rec.get("type") != "assistant":
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
            total_w = usage.get("cache_creation_input_tokens") or 0
            cc = usage.get("cache_creation") or {}
            w1h = cc.get("ephemeral_1h_input_tokens") or 0
            w5m = cc.get("ephemeral_5m_input_tokens") or 0
            if w5m + w1h < total_w:
                w5m = total_w - w1h
            reqs[mid] = {
                "session_id": rec.get("sessionId") or path.stem,
                "ts": _parse_ts(rec["timestamp"]),
                "model": model,
                "input": usage.get("input_tokens") or 0,
                "output": out,
                "cache_read": usage.get("cache_read_input_tokens") or 0,
                "write_5m": w5m,
                "write_1h": w1h,
            }
    return reqs


def ingest(conn: sqlite3.Connection, roots: list[Path]) -> int:
    """Ingest all Data roots; returns the number of Requests in the store afterwards."""
    candidates: dict[str, list[dict]] = {}
    for root in roots:
        for path in sources.main_transcripts(root):
            parsed = _parse_transcript(path)
            if not parsed:
                continue
            first_ts = min(r["ts"] for r in parsed.values())
            for mid, r in parsed.items():
                candidates.setdefault(mid, []).append({
                    "file_stem": path.stem, "session_id": r["session_id"],
                    "file_first_ts": first_ts, "root": str(root),
                    "project": path.parent.name, "req": r,
                })
    with conn:
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
    return conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
