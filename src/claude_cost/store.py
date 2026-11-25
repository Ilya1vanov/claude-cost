"""Store: the local SQLite database. One row per Request."""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  message_id TEXT PRIMARY KEY,
  data_root TEXT NOT NULL,
  project TEXT NOT NULL,
  session_id TEXT NOT NULL,
  ts_epoch REAL NOT NULL,
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cache_read_tokens INTEGER NOT NULL,
  cache_write_5m_tokens INTEGER NOT NULL,
  cache_write_1h_tokens INTEGER NOT NULL,
  priced INTEGER NOT NULL,
  cost_input REAL NOT NULL,
  cost_output REAL NOT NULL,
  cost_cache_write_5m REAL NOT NULL,
  cost_cache_write_1h REAL NOT NULL,
  cost_cache_read REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_ts ON requests (ts_epoch);

-- Incremental ingest (additive). One row per transcript file already read: `offset` is
-- the byte position up to which complete lines were consumed.
CREATE TABLE IF NOT EXISTS ingest_files (
  path TEXT PRIMARY KEY,
  data_root TEXT NOT NULL,
  project TEXT NOT NULL,
  file_stem TEXT NOT NULL,
  offset INTEGER NOT NULL,
  first_ts_epoch REAL,
  relocated INTEGER NOT NULL DEFAULT 0
);
-- other session ids a transcript links to (continued-in, resume/clear copies)
CREATE TABLE IF NOT EXISTS transcript_links (
  path TEXT NOT NULL,
  other TEXT NOT NULL,
  PRIMARY KEY (path, other)
);
-- One row per (transcript, message id): every copy of a Request, so ownership can be
-- decided across files by message id. Kept after the transcript is deleted.
CREATE TABLE IF NOT EXISTS request_copies (
  path TEXT NOT NULL,
  message_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  ts_epoch REAL NOT NULL,
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cache_read_tokens INTEGER NOT NULL,
  cache_write_5m_tokens INTEGER NOT NULL,
  cache_write_1h_tokens INTEGER NOT NULL,
  is_copy INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (path, message_id)
);
CREATE INDEX IF NOT EXISTS request_copies_mid ON request_copies (message_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- transcript stem -> the logical Session (conversation) it belongs to (ticket #3)
CREATE TABLE IF NOT EXISTS session_links (
  stem TEXT PRIMARY KEY,
  logical_session_id TEXT NOT NULL
);
-- cache event of each request's write: cold | warm | none (ticket #10, see cache.py)
CREATE TABLE IF NOT EXISTS request_cache (
  message_id TEXT PRIMARY KEY,
  event TEXT NOT NULL
);
-- per-Stream facts findings need from before their Period, stored with the cache events
-- (see cache.py): idle gap since the Stream's previous Request (NULL for its first) and the
-- context of the Stream's first Request
CREATE TABLE IF NOT EXISTS request_stream (
  message_id TEXT PRIMARY KEY,
  gap REAL,
  first_context INTEGER NOT NULL
);
-- pricing inputs not in `requests`, so a price change can re-price stored rows (ticket #5)
CREATE TABLE IF NOT EXISTS request_attrs (
  message_id TEXT PRIMARY KEY,
  speed TEXT NOT NULL,
  inference_geo TEXT NOT NULL
);
-- Explore dimensions read from the transcript (ticket #11): git branch and effort ('' = absent),
-- tools called by the request, and Skills it used (Skill tool calls, attributionSkill)
CREATE TABLE IF NOT EXISTS request_dims (
  message_id TEXT PRIMARY KEY,
  git_branch TEXT NOT NULL,
  effort TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request_tools (
  message_id TEXT NOT NULL, tool TEXT NOT NULL, PRIMARY KEY (message_id, tool)
);
CREATE TABLE IF NOT EXISTS request_skills (
  message_id TEXT NOT NULL, skill TEXT NOT NULL, PRIMARY KEY (message_id, skill)
);
CREATE TABLE IF NOT EXISTS pricing_state(id INTEGER PRIMARY KEY CHECK (id = 1), fingerprint TEXT);
-- Surface and Subagent attribution (ticket #7). copy_surface: the Surface of each copy;
-- request_surface: the owner copy's Surface, and the Subagent that made the request (if any;
-- then requests.session_id is its parent Session).
CREATE TABLE IF NOT EXISTS copy_surface (
  path TEXT NOT NULL,
  message_id TEXT NOT NULL,
  surface TEXT NOT NULL,
  PRIMARY KEY (path, message_id)
);
CREATE TABLE IF NOT EXISTS request_surface (
  message_id TEXT PRIMARY KEY,
  surface TEXT NOT NULL,
  agent_id TEXT
);
-- one row per Subagent transcript (its ingest_files.file_stem is the parent Session's stem)
CREATE TABLE IF NOT EXISTS subagent_files (
  path TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  agent_type TEXT,
  is_fork INTEGER NOT NULL,
  tool_use_id TEXT,
  parent_agent_id TEXT,
  description TEXT
);
-- Desktop session store records: CLI session ids (current first, then priorCliSessionIds)
-- and the parent Desktop session of a Side session ('side') or Fork ('fork')
CREATE TABLE IF NOT EXISTS desktop_sessions (
  desktop_id TEXT PRIMARY KEY,
  cli_ids TEXT NOT NULL,
  parent_id TEXT,
  kind TEXT
);
-- Side session / Fork links resolved to transcript stems
CREATE TABLE IF NOT EXISTS session_relations (
  child_stem TEXT PRIMARY KEY,
  parent_stem TEXT NOT NULL,
  kind TEXT NOT NULL
);
-- Spotlights (ticket #13). session_meta: what key detection reads, per main transcript stem.
-- spotlights/spotlight_pins: user-defined Spotlights and their Picked sessions (by logical
-- session id); these are user data and survive `ingest --rebuild`. `rule` is only read by the
-- one-time migration that turns rules into Picked sessions; it is '' afterwards.
-- ADR-0004: a Spotlight is identified by `id` (generated for a user-defined one, `auto:<KEY>` for
-- an Auto-spotlight); `name` is an editable label. spotlight_pins.spotlight and
-- spotlight_includes.* hold ids. auto_spotlights: per-key state of an Auto-spotlight (its custom
-- name), keyed by key, a row only once the user changed something.
CREATE TABLE IF NOT EXISTS session_meta (
  stem TEXT PRIMARY KEY,
  custom_title TEXT,
  git_branch TEXT,
  cwd TEXT,
  first_prompt TEXT
);
CREATE TABLE IF NOT EXISTS spotlights (
  name TEXT NOT NULL,
  rule TEXT NOT NULL DEFAULT '',
  id TEXT PRIMARY KEY DEFAULT ('sp-' || lower(hex(randomblob(4))))
);
CREATE TABLE IF NOT EXISTS auto_spotlights (
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  locked INTEGER NOT NULL DEFAULT 0
);
-- Dismissed sessions (ticket #67): a Session the user removed from the Auto-spotlight for `key`;
-- it stays out while it carries the key. By logical session id; user data like the pins.
CREATE TABLE IF NOT EXISTS spotlight_dismissed (
  key TEXT NOT NULL,
  session_id TEXT NOT NULL,
  PRIMARY KEY (key, session_id)
);
-- Dismissed keys (ticket #71): keys whose Auto-spotlight is hidden. Everything else about the key
-- (name, picks, dismissed sessions) stays where it is, so restoring the key brings it all back.
CREATE TABLE IF NOT EXISTS spotlight_dismissed_keys (key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS spotlight_pins (
  spotlight TEXT NOT NULL,
  session_id TEXT NOT NULL,
  PRIMARY KEY (spotlight, session_id)
);
-- Include edges (ticket #56): `spotlight` includes the Spotlight with id `included`, which may be
-- an Auto-spotlight. User data like the pins; an edge whose end no longer exists is ignored.
CREATE TABLE IF NOT EXISTS spotlight_includes (
  spotlight TEXT NOT NULL,
  included TEXT NOT NULL,
  PRIMARY KEY (spotlight, included)
);
-- compact_boundary records of main transcripts (ticket #12), deduped by record uuid (resume
-- copies repeat them); `stem` is the transcript that held it first
CREATE TABLE IF NOT EXISTS compactions (
  uuid TEXT PRIMARY KEY,
  stem TEXT NOT NULL,
  ts_epoch REAL NOT NULL,
  trigger TEXT NOT NULL,
  pre_tokens INTEGER NOT NULL,
  post_tokens INTEGER NOT NULL
);
-- compact_boundary records of Subagent transcripts (ticket #107): where a Subagent's Stream first
-- compacted, so a block priced over it stops there (`stem` = the parent transcript: agent ids can
-- repeat across Sessions); not the Session's compactions (not in `compactions`)
CREATE TABLE IF NOT EXISTS subagent_compactions (
  uuid TEXT NOT NULL,
  stem TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  ts_epoch REAL NOT NULL,
  PRIMARY KEY (stem, agent_id, uuid)  -- resumed Sessions replay the uuids of their Subagents' records
);
-- Turns (ticket #25): prompts the user typed in main transcripts, deduped by record uuid
-- (resume copies repeat them); `stem` is the transcript that held it first
CREATE TABLE IF NOT EXISTS turns (
  uuid TEXT PRIMARY KEY,
  stem TEXT NOT NULL,
  ts_epoch REAL NOT NULL
);
-- Listings (ticket #16): one row per skill / MCP server listed in a session. `stem` is the
-- (parent) transcript stem, `agent_id` '' for the main agent else the Subagent that loaded it.
-- kind skill|mcp; name = skill name or the server's display name; tokens estimated from text.
CREATE TABLE IF NOT EXISTS listings (
  stem TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  project TEXT NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  ts_epoch REAL NOT NULL,
  tokens INTEGER NOT NULL,
  PRIMARY KEY (stem, agent_id, kind, name)
);
-- Listing removals (review fix): an `mcp_instructions_delta` `removedNames` entry; the Listing
-- stops being read from ts_epoch on.
CREATE TABLE IF NOT EXISTS listing_removals (
  stem TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  ts_epoch REAL NOT NULL,
  PRIMARY KEY (stem, agent_id, kind, name, ts_epoch)
);
-- Skill calls and MCP tool calls, deduped by tool_use id; mcp name = normalised server key
CREATE TABLE IF NOT EXISTS tool_uses (
  tool_use_id TEXT PRIMARY KEY,
  stem TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  project TEXT NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  ts_epoch REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tool_uses_ts ON tool_uses (ts_epoch);
"""


class StoreError(Exception):
    """The store cannot be opened by this version; the message says why."""


class MigrationError(StoreError):
    """A migration step failed; the store was left exactly as it was."""


class StoreTooNewError(StoreError):
    """The store was written by a newer claude-cost than the running one."""


def _statements(script: str):
    """Split a SQL script into statements, so they run inside the caller's transaction
    (`executescript` would commit it)."""
    buf = ""
    for line in script.splitlines():
        if line.lstrip().startswith("--"):
            continue
        buf += line + "\n"
        if sqlite3.complete_statement(buf):
            yield buf
            buf = ""


def _to_v1(conn: sqlite3.Connection) -> None:
    """An unversioned store (or an empty one) to version 1: the ad hoc migrations, in order.
    The dropped Subagent compactions table is refilled by the next ingest, which re-reads every
    transcript while its `subagent_compactions_v2` marker is absent."""
    if any(r[1] == "uuid" and r[5] for r in conn.execute("PRAGMA table_info(subagent_compactions)")):
        conn.execute("DROP TABLE subagent_compactions")  # keyed by uuid alone
        conn.execute("DELETE FROM meta WHERE key = 'subagent_compactions_v2'")
    for statement in _statements(SCHEMA):
        conn.execute(statement)
    _migrate_spotlight_ids(conn)
    if not any(r[1] == "locked" for r in conn.execute("PRAGMA table_info(auto_spotlights)")):
        conn.execute("ALTER TABLE auto_spotlights ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")


# Ordered migration steps: step N takes a store at version N-1 to version N. To change the schema
# append a step; never edit SCHEMA or an earlier step (SCHEMA is the version-1 shape).
MIGRATIONS = [_to_v1]
LATEST_VERSION = len(MIGRATIONS)


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _too_new(found: int) -> StoreTooNewError:
    return StoreTooNewError(f"the store is at schema version {found}, newer than this claude-cost "
                            f"understands ({LATEST_VERSION}); update claude-cost, the store was not read")


def migrate(conn: sqlite3.Connection) -> bool:
    """Bring the store to LATEST_VERSION: every pending step in one transaction, so a failure
    leaves the store as it was. Returns whether anything ran."""
    found = _user_version(conn)
    if found > LATEST_VERSION:
        raise _too_new(found)
    if found == LATEST_VERSION:
        return False
    mode = conn.isolation_level
    conn.isolation_level = None  # explicit transaction control
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            found = _user_version(conn)  # another process may have migrated meanwhile
            if found > LATEST_VERSION:
                raise _too_new(found)
            for step in MIGRATIONS[found:]:
                step(conn)
            conn.execute(f"PRAGMA user_version = {LATEST_VERSION}")
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            if isinstance(e, StoreError):
                raise
            raise MigrationError(f"{e}") from e
    finally:
        conn.isolation_level = mode
    return True


def connect(path: Path, schema: bool = True) -> sqlite3.Connection:
    """`schema=False` skips migration: for per-request opens of an initialised store."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if schema:
        try:
            migrate(conn)
        except StoreError:
            conn.close()
            raise
    return conn


def backup(path: Path, version: str) -> Path:
    """Copy the store beside itself as `<name>.<version>.bak`, keeping only that one backup."""
    target = path.with_name(f"{path.name}.{version}.bak")
    source = sqlite3.connect(path)
    dest = sqlite3.connect(target)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    for old in path.parent.glob(f"{path.name}.*.bak"):
        if old != target:
            old.unlink()
    return target


def set_auto_state(conn: sqlite3.Connection, key: str, name: str | None = None,
                   locked: bool | None = None) -> None:
    """Set an Auto-spotlight's custom name and/or Locked state, keeping the per-key row only while
    it holds something: a row with the default name (the key) that is not locked is dropped."""
    conn.execute("INSERT OR IGNORE INTO auto_spotlights (key, name) VALUES (?, ?)", (key, key))
    if name is not None:
        conn.execute("UPDATE auto_spotlights SET name = ? WHERE key = ?", (name, key))
    if locked is not None:
        conn.execute("UPDATE auto_spotlights SET locked = ? WHERE key = ?", (int(locked), key))
    conn.execute("DELETE FROM auto_spotlights WHERE key = ? AND name = key AND NOT locked", (key,))


def _migrate_spotlight_ids(conn: sqlite3.Connection) -> None:
    """One-time (ADR-0004): a store whose `spotlights` table has no `id` column is name-keyed;
    give each user-defined Spotlight an id and rewrite pins and include edges from names to ids.
    A name that is no user-defined Spotlight was an Auto-spotlight's key, whose id is `auto:<KEY>`."""
    if any(r[1] == "id" for r in conn.execute("PRAGMA table_info(spotlights)")):
        return
    old = conn.execute("SELECT name, rule FROM spotlights").fetchall()
    pins = conn.execute("SELECT spotlight, session_id FROM spotlight_pins").fetchall()
    edges = conn.execute("SELECT spotlight, included FROM spotlight_includes").fetchall()
    ids = {r[0]: "sp-" + uuid.uuid4().hex[:8] for r in old}

    def to_id(name: str) -> str:
        return ids.get(name) or f"auto:{name}"
    # runs inside migrate()'s transaction
    conn.execute("DROP TABLE spotlights")
    conn.execute("CREATE TABLE spotlights (name TEXT NOT NULL, rule TEXT NOT NULL DEFAULT '', "
                 "id TEXT PRIMARY KEY DEFAULT ('sp-' || lower(hex(randomblob(4)))))")
    conn.executemany("INSERT INTO spotlights (name, rule, id) VALUES (?, ?, ?)",
                     [(n, rule, ids[n]) for n, rule in old])
    conn.execute("DELETE FROM spotlight_pins")
    conn.executemany("INSERT OR IGNORE INTO spotlight_pins VALUES (?, ?)",
                     [(to_id(n), sid) for n, sid in pins])
    conn.execute("DELETE FROM spotlight_includes")
    conn.executemany("INSERT OR IGNORE INTO spotlight_includes VALUES (?, ?)",
                     [(to_id(a), to_id(b)) for a, b in edges])
