"""Ingest: reads main transcripts of each Data root into the store.

Incremental: each transcript is read only from the byte offset where the last run
stopped (complete lines only). Every copy of a Request is kept in `request_copies`, and the
owner of a message id is decided across all copies by `ownership.pick_owner`, so an
appended file never re-adds or doubles a Request. Rows are never deleted when a
transcript disappears; `rebuild=True` re-reads every transcript that still exists and
recomputes all Requests from the kept copies.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import cache, ownership, pricing, sources


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


RETENTION_DAYS = 60


@dataclass
class Parsed:
    reqs: dict[str, dict] = field(default_factory=dict)
    relocated: bool = False
    links: set[str] = field(default_factory=set)  # other session ids of the same conversation


def _parse_lines(lines: list[bytes], stem: str) -> Parsed:
    """Group assistant lines by message id: first line wins, output = max over lines."""
    parsed = Parsed()
    reqs = parsed.reqs
    for raw in lines:
        try:
            rec = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue  # corrupt line
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
        try:
            ts = _parse_ts(rec["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        reqs[mid] = {
            "session_id": sid,
            "is_copy": bool(fork_sid),
            "ts": ts,
            "model": model,
            "input": usage.get("input_tokens") or 0,
            "output": out,
            "cache_read": usage.get("cache_read_input_tokens") or 0,
            "write_5m": w5m,
            "write_1h": w1h,
        }
    return parsed


def _read_new(path: Path, offset: int) -> tuple[list[bytes], int]:
    """Complete lines after `offset` and the new offset. A trailing partial line is left."""
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read()
    end = data.rfind(b"\n") + 1
    return data[:end].splitlines(), offset + end


def _store_copies(conn: sqlite3.Connection, path: Path, reqs: dict[str, dict]) -> None:
    for mid, r in reqs.items():
        # Existing copy (earlier part of the same file): keep its first line, raise output.
        conn.execute(
            "INSERT INTO request_copies VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path, message_id) DO UPDATE SET "
            "output_tokens = MAX(output_tokens, excluded.output_tokens)",
            (str(path), mid, r["session_id"], r["ts"].timestamp(), r["model"], r["input"],
             r["output"], r["cache_read"], r["write_5m"], r["write_1h"],
             1 if r["is_copy"] else 0))


def _epoch_dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc)


def _recompute(conn: sqlite3.Connection, mids: list[str]) -> None:
    """(Re)decide the owner of each message id from all kept copies and upsert the Request."""
    for mid in mids:
        rows = conn.execute(
            "SELECT c.*, f.data_root, f.project, f.file_stem, f.first_ts_epoch, f.relocated "
            "FROM request_copies c JOIN ingest_files f ON f.path = c.path "
            "WHERE c.message_id = ?", (mid,)).fetchall()
        if not rows:
            continue
        cands = [{
            "file_stem": row["file_stem"], "session_id": row["session_id"],
            "file_first_ts": _epoch_dt(row["first_ts_epoch"]), "root": row["data_root"],
            "project": row["project"], "is_copy": bool(row["is_copy"]),
            "relocated": bool(row["relocated"]),
            "req": {"session_id": row["session_id"], "ts": _epoch_dt(row["ts_epoch"]),
                    "model": row["model"], "input": row["input_tokens"],
                    "output": row["output_tokens"], "cache_read": row["cache_read_tokens"],
                    "write_5m": row["cache_write_5m_tokens"],
                    "write_1h": row["cache_write_1h_tokens"]},
        } for row in rows]
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


def _regroup_sessions(conn: sqlite3.Connection) -> None:
    """Rebuild `session_links` from every transcript ever read (vanished ones included)."""
    first_ts: dict[str, datetime] = {}
    for row in conn.execute("SELECT file_stem, first_ts_epoch FROM ingest_files "
                            "WHERE first_ts_epoch IS NOT NULL"):
        ts = _epoch_dt(row["first_ts_epoch"])
        first_ts[row["file_stem"]] = min(ts, first_ts.get(row["file_stem"], ts))
    links = {(row["stem"], row["other"]) for row in conn.execute(
        "SELECT f.file_stem AS stem, l.other FROM transcript_links l "
        "JOIN ingest_files f ON f.path = l.path")}
    conn.execute("DELETE FROM session_links")
    conn.executemany("INSERT INTO session_links VALUES (?,?)",
                     _group_sessions(first_ts, links).items())


def is_first_run(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM meta WHERE key = 'last_ingest_epoch'").fetchone() is None


def last_ingest_epoch(conn: sqlite3.Connection) -> float | None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'last_ingest_epoch'").fetchone()
    return float(row["value"]) if row else None


def retention_warnings(roots: list[Path]) -> list[str]:
    """Warn per Data root when Claude Code would delete transcripts sooner than 60 days.

    Reads `cleanupPeriodDays` from `<root>/settings.json` and `settings.local.json`; absent
    means Claude Code's default of 30 days.
    """
    warnings = []
    for root in roots:
        days = None
        for name in ("settings.json", "settings.local.json"):
            try:
                value = json.loads((root / name).read_text()).get("cleanupPeriodDays")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                days = value
        shown = "unset (default 30)" if days is None else str(days)
        if (30 if days is None else days) < RETENTION_DAYS:
            warnings.append(
                f"warning: cleanupPeriodDays is {shown} in {root}; Claude Code deletes "
                f"transcripts older than that, so history before ingest is lost. "
                f"Set it to {RETENTION_DAYS} or more.")
    return warnings


def ingest(conn: sqlite3.Connection, roots: list[Path], *, rebuild: bool = False,
           now: datetime | None = None) -> int:
    """Ingest all Data roots; returns the number of Requests in the store afterwards."""
    now = now or datetime.now(timezone.utc)
    touched: set[str] = set()
    changed = rebuild
    with conn:
        if rebuild:
            # Forget what we read from files that still exist; copies from vanished
            # transcripts are history and stay. Requests are recomputed below.
            existing = {str(p) for root in roots for p in sources.main_transcripts(root)}
            for (path,) in conn.execute("SELECT path FROM ingest_files").fetchall():
                if path in existing:
                    for table in ("request_copies", "transcript_links", "ingest_files"):
                        conn.execute(f"DELETE FROM {table} WHERE path = ?", (path,))
            touched.update(r[0] for r in conn.execute(
                "SELECT message_id FROM requests").fetchall())
        for root in roots:
            for path in sources.main_transcripts(root):
                row = conn.execute("SELECT offset FROM ingest_files WHERE path = ?",
                                   (str(path),)).fetchone()
                offset = row["offset"] if row else 0
                if path.stat().st_size < offset:  # truncated or replaced: read it afresh
                    for table in ("request_copies", "transcript_links"):
                        conn.execute(f"DELETE FROM {table} WHERE path = ?", (str(path),))
                    offset = 0
                lines, new_offset = _read_new(path, offset)
                if row and new_offset == offset:
                    continue
                changed = True
                parsed = _parse_lines(lines, path.stem)
                first = min((r["ts"].timestamp() for r in parsed.reqs.values()), default=None)
                conn.execute(
                    "INSERT INTO ingest_files VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET offset = excluded.offset, "
                    "relocated = MAX(relocated, excluded.relocated), "
                    "first_ts_epoch = CASE WHEN first_ts_epoch IS NULL THEN excluded.first_ts_epoch "
                    "WHEN excluded.first_ts_epoch IS NULL THEN first_ts_epoch "
                    "ELSE MIN(first_ts_epoch, excluded.first_ts_epoch) END",
                    (str(path), str(root), path.parent.name, path.stem, new_offset, first,
                     1 if parsed.relocated else 0))
                conn.executemany("INSERT OR IGNORE INTO transcript_links VALUES (?,?)",
                                 [(str(path), other) for other in parsed.links])
                _store_copies(conn, path, parsed.reqs)
                touched.update(parsed.reqs)
                if parsed.relocated:  # a relocated flag changes ownership of all its requests
                    touched.update(r[0] for r in conn.execute(
                        "SELECT message_id FROM request_copies WHERE path = ?", (str(path),)))
        # A file with no requests yet has no first_ts; it has no copies either, so the
        # join in _recompute never sees it.
        _recompute(conn, sorted(touched))
        _regroup_sessions(conn)
        if changed or not conn.execute("SELECT 1 FROM request_cache LIMIT 1").fetchone():
            cache.classify_all(conn)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_ingest_epoch', ?)",
                     (str(now.timestamp()),))
    return conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
