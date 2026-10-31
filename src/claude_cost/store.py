"""Store: the local SQLite database. One row per Request."""
from __future__ import annotations

import sqlite3
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
-- Spotlights (ticket #13). session_meta: what rules and key detection read, per main
-- transcript stem. spotlights/spotlight_pins: user-defined Spotlights (a rule and/or pinned
-- Sessions, by logical session id); these are user data and survive `ingest --rebuild`.
CREATE TABLE IF NOT EXISTS session_meta (
  stem TEXT PRIMARY KEY,
  custom_title TEXT,
  git_branch TEXT,
  cwd TEXT,
  first_prompt TEXT
);
CREATE TABLE IF NOT EXISTS spotlights (
  name TEXT PRIMARY KEY,
  rule TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS spotlight_pins (
  spotlight TEXT NOT NULL,
  session_id TEXT NOT NULL,
  PRIMARY KEY (spotlight, session_id)
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
"""


def connect(path: Path, schema: bool = True) -> sqlite3.Connection:
    """`schema=False` skips the CREATE script: for per-request opens of an initialised store."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if schema:
        conn.executescript(SCHEMA)
    return conn
