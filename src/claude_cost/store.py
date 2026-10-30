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
-- transcript stem -> the logical Session (conversation) it belongs to (ticket #3)
CREATE TABLE IF NOT EXISTS session_links (
  stem TEXT PRIMARY KEY,
  logical_session_id TEXT NOT NULL
);
-- Surface and subagent attribution per request (ticket #7)
CREATE TABLE IF NOT EXISTS request_attrs (
  message_id TEXT PRIMARY KEY,
  surface TEXT NOT NULL,
  agent_id TEXT  -- set when a Subagent made the request; requests.session_id is its parent Session
);
CREATE TABLE IF NOT EXISTS subagents (
  session_stem TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  agent_type TEXT,
  is_fork INTEGER NOT NULL,
  tool_use_id TEXT,
  parent_agent_id TEXT,
  description TEXT,
  PRIMARY KEY (session_stem, agent_id)
);
-- Side session / Fork links resolved to transcript stems from the Desktop store
CREATE TABLE IF NOT EXISTS session_relations (
  child_stem TEXT PRIMARY KEY,
  parent_stem TEXT NOT NULL,
  kind TEXT NOT NULL  -- 'side' | 'fork'
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
