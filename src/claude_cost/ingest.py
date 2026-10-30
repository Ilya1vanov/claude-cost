"""Ingest: reads main and Subagent transcripts of each Data root into the store.

A Subagent transcript is ingested like a main one but belongs to its parent Session
(its `file_stem` is the parent's stem); `subagent_files` marks it and holds its metadata.

Incremental: each transcript is read only from the byte offset where the last run
stopped (complete lines only). Every copy of a Request is kept in `request_copies`, and the
owner of a message id is decided across all copies by `ownership.pick_owner`, so an
appended file never re-adds or doubles a Request. Rows are never deleted when a
transcript disappears; `rebuild=True` re-reads every transcript that still exists and
recomputes all Requests from the kept copies.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import cache, ownership, pricing, sources
from .localtime import price_day


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


RETENTION_DAYS = 60

CHARS_PER_TOKEN = 3.6  # Listing size estimate: characters / 3.6


def listing_tokens(text: str) -> int:
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def server_key(name: str) -> str:
    """Normalise an MCP server (display name or the `mcp__<server>__` segment) for matching."""
    return "".join(c if c.isalnum() else "_" for c in name).lower()


def _attachment_listings(att: dict, ts: float) -> list[tuple]:
    kind = att.get("type")
    out = []
    if kind == "skill_listing":
        names = [n for n in att.get("names") or [] if isinstance(n, str)]
        lines = [ln for ln in str(att.get("content") or "").splitlines() if ln.startswith("- ")]
        for n in names:
            line = next((ln for ln in lines if ln.startswith(f"- {n}:") or ln == f"- {n}"), n)
            out.append(("skill", n, ts, listing_tokens(line)))
    elif kind == "mcp_instructions_delta":
        names = att.get("addedNames") or []
        blocks = att.get("addedBlocks") or []
        for n, b in zip(names, blocks):
            if isinstance(n, str) and isinstance(b, str):
                out.append(("mcp", server_key(n), ts, listing_tokens(b)))
    return out


def _attachment_removals(att: dict, ts: float) -> list[tuple]:
    if att.get("type") != "mcp_instructions_delta":
        return []
    return [("mcp", server_key(n), ts) for n in att.get("removedNames") or []
            if isinstance(n, str)]


def _tool_uses(rec: dict, ts: float) -> list[tuple]:
    content = (rec.get("message") or {}).get("content")
    out = []
    for blk in content if isinstance(content, list) else []:
        if not isinstance(blk, dict) or blk.get("type") != "tool_use" or not blk.get("id"):
            continue
        name = blk.get("name")
        if name == "Skill":
            skill = (blk.get("input") or {}).get("skill")
            if isinstance(skill, str) and skill:
                out.append((str(blk["id"]), "skill", skill, ts))
        elif isinstance(name, str) and name.startswith("mcp__") and name.count("__") >= 2:
            out.append((str(blk["id"]), "mcp", server_key(name[5:].split("__")[0]), ts))
    return out

_SURFACES = {"cli": "CLI", "claude-desktop": "Desktop", "sdk-cli": "SDK"}


def surface_of(entrypoint: object) -> str:
    """Map a record's `entrypoint` to a Surface; anything unrecognised is "Other"."""
    if isinstance(entrypoint, str):
        if entrypoint in _SURFACES:
            return _SURFACES[entrypoint]
        if entrypoint.startswith("sdk"):
            return "SDK"
    return "Other"


@dataclass
class Parsed:
    reqs: dict[str, dict] = field(default_factory=dict)
    relocated: bool = False
    links: set[str] = field(default_factory=set)  # other session ids of the same conversation
    compactions: list[tuple] = field(default_factory=list)  # (uuid, ts, trigger, pre, post)
    turns: list[tuple] = field(default_factory=list)  # (uuid, ts): prompts the user typed
    listings: list[tuple] = field(default_factory=list)  # (kind, name, ts, tokens)
    removals: list[tuple] = field(default_factory=list)  # (kind, name, ts)
    uses: list[tuple] = field(default_factory=list)  # (tool_use_id, kind, name, ts)
    # Spotlight inputs (ticket #13): last custom title; first cwd, git branch and prompt
    title: str | None = None
    cwd: str | None = None
    branch: str | None = None
    prompt: str | None = None


def _prompt_text(rec: dict) -> str | None:
    """The real user prompt of a `user` record (not meta, not an injected `<...>` wrapper)."""
    if rec.get("type") != "user" or rec.get("isMeta"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        content = next((b.get("text") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"), None)
    if isinstance(content, str) and content.strip() and not content.lstrip().startswith("<"):
        return content[:300]
    return None


def _typed_text(text: object) -> bool:
    """Text a person typed: plain, or a slash command; not an injected `<...>` wrapper."""
    if not isinstance(text, str) or not text.strip():
        return False
    text = text.lstrip()
    return not text.startswith("<") or text.startswith(("<command-name>", "<command-message>"))


def _turn(rec: dict) -> tuple[str, float] | None:
    """(uuid, ts) when the record is a prompt the user typed: a `user` record or a
    `queued_command` attachment (sent while Claude was working). Tool results, compaction
    summaries, hook/system injections, task notifications and Subagent messages are not."""
    kind = rec.get("type")
    if rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("isSidechain") \
            or "forkedFrom" in rec:
        return None
    if kind == "user":
        text = (rec.get("message") or {}).get("content")
        if isinstance(text, list):  # blocks: the typed text, ignoring images; tool results have none
            text = next((b.get("text") for b in text
                         if isinstance(b, dict) and b.get("type") == "text"), None)
    elif kind == "attachment" and (rec.get("attachment") or {}).get("type") == "queued_command":
        text = rec["attachment"].get("prompt")
    else:
        return None
    origin = rec.get("origin") or (rec.get("attachment") or {}).get("origin")
    if isinstance(origin, dict) and origin.get("kind") != "human":
        return None
    if not _typed_text(text) or not isinstance(rec.get("uuid"), str):
        return None
    try:
        return rec["uuid"], _parse_ts(rec["timestamp"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def _scan_meta(rec: dict, parsed: Parsed) -> None:
    title = rec.get("customTitle")
    if rec.get("type") == "custom-title" and isinstance(title, str) and title:
        parsed.title = title  # repeated; the last one wins
    for key, attr in (("cwd", "cwd"), ("gitBranch", "branch")):
        if getattr(parsed, attr) is None and isinstance(rec.get(key), str) and rec[key]:
            setattr(parsed, attr, rec[key])
    if parsed.prompt is None:
        parsed.prompt = _prompt_text(rec)


def _str_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""


def _tool_use(content: object, attribution_skill: object) -> tuple[set[str], set[str]]:
    """Tool names called in a message's content blocks, and the Skills used (Skill tool calls
    plus the line's `attributionSkill`)."""
    tools: set[str] = set()
    skills = {attribution_skill} if isinstance(attribution_skill, str) and attribution_skill else set()
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if isinstance(name, str) and name:
            tools.add(name)
            inp = block.get("input")
            if name == "Skill" and isinstance(inp, dict) and isinstance(inp.get("skill"), str):
                skills.add(inp["skill"])
    return tools, skills


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
        if "forkedFrom" not in rec:  # copied Fork history is the parent's, not this session's
            _scan_meta(rec, parsed)
        kind = rec.get("type")
        if kind == "relocated":
            parsed.relocated = True
            continue
        if kind == "continued-in":
            nxt = rec.get("continuedInSessionId")
            if isinstance(nxt, str) and nxt:
                parsed.links.add(nxt)
            continue
        if kind == "system" and rec.get("subtype") == "compact_boundary":
            meta = rec.get("compactMetadata")
            meta = meta if isinstance(meta, dict) else {}
            try:
                parsed.compactions.append((
                    str(rec["uuid"]), _parse_ts(rec["timestamp"]).timestamp(),
                    str(meta.get("trigger") or "manual"),
                    int(meta.get("preTokens") or 0), int(meta.get("postTokens") or 0)))
            except (KeyError, TypeError, ValueError):
                pass
            continue
        turn = _turn(rec)
        if turn:
            parsed.turns.append(turn)
        if kind == "attachment":
            att = rec.get("attachment")
            if isinstance(att, dict):
                try:
                    ts = _parse_ts(rec["timestamp"]).timestamp()
                    parsed.listings.extend(_attachment_listings(att, ts))
                    parsed.removals.extend(_attachment_removals(att, ts))
                except (KeyError, TypeError, ValueError):
                    pass
            continue
        if kind != "assistant":
            continue
        try:
            parsed.uses.extend(_tool_uses(rec, _parse_ts(rec["timestamp"]).timestamp()))
        except (KeyError, TypeError, ValueError):
            pass
        msg = rec.get("message") or {}
        usage = msg.get("usage")
        model = msg.get("model")
        mid = msg.get("id")
        if not usage or not mid or not model or model == "<synthetic>":
            continue
        out = usage.get("output_tokens") or 0
        tools, skills = _tool_use(msg.get("content"), rec.get("attributionSkill"))
        if mid in reqs:
            reqs[mid]["output"] = max(reqs[mid]["output"], out)
            reqs[mid]["tools"] |= tools  # one streamed line per content block: union them
            reqs[mid]["skills"] |= skills
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
            "surface": surface_of(rec.get("entrypoint")),
            "speed": usage.get("speed") or "standard",
            "inference_geo": usage.get("inference_geo") or "",
            "git_branch": _str_or_empty(rec.get("gitBranch")),
            "effort": _str_or_empty(rec.get("effort")),
            "tools": tools,
            "skills": skills,
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
        conn.execute("INSERT OR IGNORE INTO request_attrs VALUES (?,?,?)",
                     (mid, r["speed"], r["inference_geo"]))
        conn.execute("INSERT OR IGNORE INTO copy_surface VALUES (?,?,?)",
                     (str(path), mid, r["surface"]))
        conn.execute("INSERT OR IGNORE INTO request_dims VALUES (?,?,?)",
                     (mid, r["git_branch"], r["effort"]))
        conn.executemany("INSERT OR IGNORE INTO request_tools VALUES (?,?)",
                         [(mid, t) for t in sorted(r["tools"])])
        conn.executemany("INSERT OR IGNORE INTO request_skills VALUES (?,?)",
                         [(mid, s) for s in sorted(r["skills"])])


def _epoch_dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc)


def _recompute(conn: sqlite3.Connection, mids: list[str], table: dict) -> None:
    """(Re)decide the owner of each message id from all kept copies and upsert the Request."""
    for mid in mids:
        rows = conn.execute(
            "SELECT c.*, f.data_root, f.project, f.file_stem, f.first_ts_epoch, f.relocated, "
            "s.agent_id, cs.surface "
            "FROM request_copies c JOIN ingest_files f ON f.path = c.path "
            "LEFT JOIN subagent_files s ON s.path = c.path "
            "LEFT JOIN copy_surface cs ON cs.path = c.path AND cs.message_id = c.message_id "
            "WHERE c.message_id = ?", (mid,)).fetchall()
        if not rows:
            continue
        cands = [{
            "file_stem": row["file_stem"], "session_id": row["session_id"],
            "file_first_ts": _epoch_dt(row["first_ts_epoch"]), "root": row["data_root"],
            "project": row["project"], "is_copy": bool(row["is_copy"]),
            "relocated": bool(row["relocated"]),
            "is_subagent": row["agent_id"] is not None, "agent_id": row["agent_id"],
            "surface": row["surface"] or "Other",
            "req": {"session_id": row["session_id"], "ts": _epoch_dt(row["ts_epoch"]),
                    "model": row["model"], "input": row["input_tokens"],
                    "output": row["output_tokens"], "cache_read": row["cache_read_tokens"],
                    "write_5m": row["cache_write_5m_tokens"],
                    "write_1h": row["cache_write_1h_tokens"]},
        } for row in rows]
        owner = ownership.pick_owner(cands)
        r = owner["req"]
        attrs = conn.execute("SELECT speed, inference_geo FROM request_attrs WHERE message_id = ?",
                             (mid,)).fetchone()
        cost = pricing.price_request(
            r["model"], price_day(r["ts"].timestamp()), input=r["input"], output=r["output"],
            cache_read=r["cache_read"], write_5m=r["write_5m"], write_1h=r["write_1h"],
            speed=attrs["speed"] if attrs else None,
            inference_geo=attrs["inference_geo"] if attrs else None, table=table,
        )
        c = cost or pricing.CostComponents()
        conn.execute("INSERT OR REPLACE INTO request_surface VALUES (?,?,?)",
                     (mid, owner["surface"], owner["agent_id"]))
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
            r["model"], price_day(r["ts_epoch"]),
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


def _desktop_signature(stores: list[Path]) -> str:
    """Path, mtime and size of every Desktop store file: changes when any of them does."""
    files = []
    for store in stores:
        for p in sorted(store.rglob("local_*.json")):
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((str(p), st.st_mtime_ns, st.st_size))
    return json.dumps(files)


def _store_desktop(conn: sqlite3.Connection, stores: list[Path]) -> None:
    """Replace the kept Desktop session records with those found in `stores`."""
    conn.execute("DELETE FROM desktop_sessions")
    for store in stores:
        for rec in sources.desktop_sessions(store):
            ids = [rec["cliSessionId"]] if isinstance(rec.get("cliSessionId"), str) else []
            prior = rec.get("priorCliSessionIds")
            if isinstance(prior, list):
                ids += [i for i in prior if isinstance(i, str)]
            spawned = rec.get("spawnedFrom")
            if isinstance(spawned, dict):
                parent, kind = spawned.get("sessionId"), "side"
            else:
                parent, kind = rec.get("forkedFromSessionId"), "fork"
            conn.execute("INSERT OR REPLACE INTO desktop_sessions VALUES (?,?,?,?)",
                         (rec["sessionId"], json.dumps(ids),
                          parent if isinstance(parent, str) else None,
                          kind if isinstance(parent, str) else None))


def _desktop_links(conn: sqlite3.Connection, known: set[str]):
    """From the kept Desktop records: (stem pairs of one Desktop session, {child: (parent, kind)}).

    `priorCliSessionIds` are earlier transcripts of the same Desktop session. A Side session
    (`spawnedFrom`) or Fork (`forkedFromSessionId`) names a parent Desktop session, resolved
    to the first of its CLI session ids that has a transcript. Unresolvable ends stay unlinked.
    """
    ids = {row["desktop_id"]: json.loads(row["cli_ids"])
           for row in conn.execute("SELECT desktop_id, cli_ids FROM desktop_sessions")}
    group = {(v[0], other) for v in ids.values() for other in v[1:]}

    def resolve(desktop_id: object) -> str | None:
        return next((i for i in ids.get(desktop_id, []) if i in known), None)  # type: ignore[call-overload]

    relations: dict[str, tuple[str, str]] = {}
    for row in conn.execute("SELECT desktop_id, parent_id, kind FROM desktop_sessions "
                            "WHERE parent_id IS NOT NULL ORDER BY desktop_id"):
        child, parent = resolve(row["desktop_id"]), resolve(row["parent_id"])
        if child and parent and child != parent:
            relations[child] = (parent, row["kind"])
    return group, relations


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
    group_pairs, relations = _desktop_links(conn, set(first_ts))
    conn.execute("DELETE FROM session_links")
    conn.executemany("INSERT INTO session_links VALUES (?,?)",
                     _group_sessions(first_ts, links | group_pairs).items())
    conn.execute("DELETE FROM session_relations")
    conn.executemany("INSERT INTO session_relations VALUES (?,?,?)",
                     [(c, p, k) for c, (p, k) in relations.items()])


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


def _transcripts(root: Path) -> list[tuple[Path, str, str, dict | None]]:
    """(path, project, session stem, subagent metadata or None) for main and Subagent transcripts."""
    out: list[tuple[Path, str, str, dict | None]] = [
        (p, p.parent.name, p.stem, None) for p in sources.main_transcripts(root)]
    out += [(s.path, s.project, s.session_stem, {"agent_id": s.agent_id, **s.meta})
            for s in sources.subagent_transcripts(root)]
    return out


def _str(meta: dict, key: str) -> str | None:
    return meta[key] if isinstance(meta.get(key), str) else None


def ingest(conn: sqlite3.Connection, roots: list[Path], *, rebuild: bool = False,
           now: datetime | None = None, price_table: dict | None = None,
           desktop_stores: list[Path] | None = None) -> int:
    """Ingest all Data roots; returns the number of Requests in the store afterwards.

    `desktop_stores` are Desktop session store folders (scanned recursively) that link Side
    sessions, Forks and earlier transcripts of the same Desktop session. A list replaces
    the kept Desktop records (empty = no Desktop links); None leaves them as they are.
    """
    price_table = pricing.PRICE_TABLE if price_table is None else price_table
    now = now or datetime.now(timezone.utc)
    touched: set[str] = set()
    if (not rebuild and conn.execute("SELECT 1 FROM ingest_files LIMIT 1").fetchone()
            and not conn.execute("SELECT 1 FROM meta WHERE key = 'explore_dims'").fetchone()):
        rebuild = True  # a store from before the Explore attributes: re-read once to fill them
    if (not rebuild and conn.execute("SELECT 1 FROM ingest_files LIMIT 1").fetchone()
            and not conn.execute("SELECT 1 FROM meta WHERE key = 'turns'").fetchone()):
        rebuild = True  # a store from before Turns: re-read once to record them
    changed = rebuild
    with conn:
        if rebuild:
            # Forget what we read from files that still exist; copies from vanished
            # transcripts are history and stay. Requests are recomputed below.
            existing = {str(t[0]) for root in roots for t in _transcripts(root)}
            for (path,) in conn.execute("SELECT path FROM ingest_files").fetchall():
                if path in existing:
                    for table in ("request_copies", "transcript_links", "ingest_files"):
                        conn.execute(f"DELETE FROM {table} WHERE path = ?", (path,))
            touched.update(r[0] for r in conn.execute(
                "SELECT message_id FROM requests").fetchall())
        for root in roots:
            for path, project, stem, sub in _transcripts(root):
                if sub is not None:  # Subagent metadata may be written after the transcript
                    conn.execute(
                        "INSERT OR REPLACE INTO subagent_files VALUES (?,?,?,?,?,?,?)",
                        (str(path), sub["agent_id"], _str(sub, "agentType"),
                         1 if sub.get("isFork") is True or sub.get("agentType") == "fork" else 0,
                         _str(sub, "toolUseId"), _str(sub, "parentAgentId"),
                         _str(sub, "description")))
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
                parsed = _parse_lines(lines, stem)
                first = min((r["ts"].timestamp() for r in parsed.reqs.values()), default=None)
                conn.execute(
                    "INSERT INTO ingest_files VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET offset = excluded.offset, "
                    "relocated = MAX(relocated, excluded.relocated), "
                    "first_ts_epoch = CASE WHEN first_ts_epoch IS NULL THEN excluded.first_ts_epoch "
                    "WHEN excluded.first_ts_epoch IS NULL THEN first_ts_epoch "
                    "ELSE MIN(first_ts_epoch, excluded.first_ts_epoch) END",
                    (str(path), str(root), project, stem, new_offset, first,
                     1 if parsed.relocated else 0))
                conn.executemany("INSERT OR IGNORE INTO transcript_links VALUES (?,?)",
                                 [(str(path), other) for other in parsed.links])
                _store_copies(conn, path, parsed.reqs)
                if sub is None:  # Spotlight metadata of main transcripts only
                    conn.execute(
                        "INSERT INTO session_meta VALUES (?,?,?,?,?) ON CONFLICT(stem) DO UPDATE SET "
                        "custom_title = COALESCE(excluded.custom_title, custom_title), "
                        "git_branch = COALESCE(git_branch, excluded.git_branch), "
                        "cwd = COALESCE(cwd, excluded.cwd), "
                        "first_prompt = COALESCE(first_prompt, excluded.first_prompt)",
                        (stem, parsed.title, parsed.branch, parsed.cwd, parsed.prompt))
                if sub is None:  # a Subagent's own compactions are not the Session's
                    conn.executemany("INSERT OR IGNORE INTO compactions "
                                     "VALUES (?,?,?,?,?,?)",
                                     [(u, stem, ts, trig, pre, post)
                                      for u, ts, trig, pre, post in parsed.compactions])
                if sub is None:  # Turns: main transcripts only, deduped by record uuid
                    conn.executemany("INSERT OR IGNORE INTO turns VALUES (?,?,?)",
                                     [(u, stem, ts) for u, ts in parsed.turns])
                agent = sub["agent_id"] if sub is not None else ""
                conn.executemany("INSERT OR IGNORE INTO listings VALUES (?,?,?,?,?,?,?)",
                                 [(stem, agent, project, k, n, ts, tok)
                                  for k, n, ts, tok in parsed.listings])
                conn.executemany("INSERT OR IGNORE INTO listing_removals VALUES (?,?,?,?,?)",
                                 [(stem, agent, k, n, ts) for k, n, ts in parsed.removals])
                conn.executemany("INSERT OR IGNORE INTO tool_uses VALUES (?,?,?,?,?,?,?)",
                                 [(uid, stem, agent, project, k, n, ts)
                                  for uid, k, n, ts in parsed.uses])
                touched.update(parsed.reqs)
                if parsed.relocated:  # a relocated flag changes ownership of all its requests
                    touched.update(r[0] for r in conn.execute(
                        "SELECT message_id FROM request_copies WHERE path = ?", (str(path),)))
        # A file with no requests yet has no first_ts; it has no copies either, so the
        # join in _recompute never sees it.
        _recompute(conn, sorted(touched), price_table)
        desktop_changed = False
        if desktop_stores is not None:
            sig = _desktop_signature(desktop_stores)
            row = conn.execute("SELECT value FROM meta WHERE key = 'desktop_sig'").fetchone()
            if rebuild or row is None or row["value"] != sig:
                _store_desktop(conn, desktop_stores)
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('desktop_sig', ?)", (sig,))
                desktop_changed = True
        if changed or desktop_changed or not conn.execute("SELECT 1 FROM session_links LIMIT 1").fetchone():
            _regroup_sessions(conn)
        if changed or desktop_changed or not conn.execute("SELECT 1 FROM request_cache LIMIT 1").fetchone():
            cache.classify_all(conn)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_ingest_epoch', ?)",
                     (str(now.timestamp()),))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('explore_dims', '1')")
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('turns', '1')")
    reprice(conn, price_table)
    return conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
