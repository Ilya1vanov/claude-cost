"""Fixture harness: builds a small synthetic Data root per test.

Usage:
    root = DataRoot(tmp_path / "claude")
    root.write_transcript("proj-a", "sess-1", [req("msg_1", "2026-10-29T10:00:00Z", output=500)])
Then ingest and assert through the QueryService.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Req:
    message_id: str
    ts: str
    model: str = "claude-opus-5-5"
    input: int = 0
    output: int = 0
    cache_read: int = 0
    write_5m: int = 0
    write_1h: int = 0
    lines: int = 1  # streaming split: one JSONL line per content block
    session_id: str | None = None  # record sessionId; default = transcript session


def req(message_id: str, ts: str, **kw) -> Req:
    return Req(message_id, ts, **kw)


def assistant_lines(r: Req, session_id: str) -> list[dict]:
    out = []
    for i in range(r.lines):
        usage = {
            "input_tokens": r.input,
            # streamed output grows; the last line carries the final value
            "output_tokens": r.output if i == r.lines - 1 else r.output // 2,
            "cache_read_input_tokens": r.cache_read,
            "cache_creation_input_tokens": r.write_5m + r.write_1h,
            "cache_creation": {"ephemeral_5m_input_tokens": r.write_5m,
                               "ephemeral_1h_input_tokens": r.write_1h},
        }
        out.append({
            "type": "assistant", "uuid": f"{r.message_id}-u{i}", "timestamp": r.ts,
            "sessionId": r.session_id or session_id,
            "message": {"id": r.message_id, "model": r.model, "role": "assistant",
                        "content": [{"type": "text", "text": "x"}], "usage": usage},
        })
    return out


class DataRoot:
    """A synthetic ~/.claude. Always a path under pytest's tmp_path, never the real one."""

    def __init__(self, path: Path):
        self.path = path
        (path / "projects").mkdir(parents=True, exist_ok=True)

    def write_transcript(self, project: str, session_id: str, reqs: list[Req],
                         extra_lines: list[str] | None = None) -> Path:
        d = self.path / "projects" / project
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{session_id}.jsonl"
        lines = [json.dumps(rec) for r in reqs for rec in assistant_lines(r, session_id)]
        f.write_text("\n".join(lines + (extra_lines or [])) + "\n")
        return f

    def append_to_transcript(self, project: str, session_id: str, reqs: list[Req],
                             raw: str = "") -> Path:
        """Append records to an existing transcript (as Claude Code does while a session runs)."""
        f = self.path / "projects" / project / f"{session_id}.jsonl"
        lines = [json.dumps(rec) for r in reqs for rec in assistant_lines(r, session_id)]
        with f.open("a") as fh:
            fh.write("\n".join(lines) + ("\n" if lines else "") + raw)
        return f

    def write_subagent_transcript(self, project: str, session_id: str, agent_id: str,
                                  reqs: list[Req]) -> Path:
        d = self.path / "projects" / project / session_id / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"agent-{agent_id}.jsonl"
        f.write_text("\n".join(json.dumps(rec) for r in reqs
                               for rec in assistant_lines(r, session_id)) + "\n")
        return f
