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
class Advisor:
    """One advisor iteration of a Request: the advisor's model and its own billed tokens."""
    model: str = "claude-opus-5-5"
    input: int = 0
    output: int = 0
    cache_read: int = 0
    write_5m: int = 0
    write_1h: int = 0


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
    forked_from: str | None = None  # marks copied fork history: forkedFrom.sessionId
    entrypoint: str | None = None  # record entrypoint: cli | claude-desktop | sdk-cli
    speed: str | None = None  # usage.speed ("fast"); None = key absent
    inference_geo: str | None = None  # usage.inference_geo ("us", "global"); None = absent
    git_branch: str | None = None  # record gitBranch
    effort: str | None = None  # record effort ("low" | "medium" | "high")
    tools: tuple[str, ...] = ()  # tool_use blocks (names) on the first streamed line
    skills: tuple[str, ...] = ()  # Skill tool calls (input.skill) on the last streamed line
    text: str = "x"  # assistant text block, on the first streamed line ("" = none)
    tool_calls: tuple[tuple[str, dict], ...] = ()  # (name, input) tool_use blocks, last streamed line
    advisor: Advisor | None = None  # usage.iterations = [message, advisor_message, message]


def req(message_id: str, ts: str, **kw) -> Req:
    return Req(message_id, ts, **kw)


def _iterations(r: Req) -> list[dict]:
    """[message, advisor_message, message]: the executor's tokens split over its two passes, so
    the top-level usage is their sum."""
    def message(inp, out, cr, w5, w1) -> dict:
        return {"type": "message", "input_tokens": inp, "output_tokens": out,
                "cache_read_input_tokens": cr, "cache_creation_input_tokens": w5 + w1,
                "cache_creation": {"ephemeral_5m_input_tokens": w5,
                                   "ephemeral_1h_input_tokens": w1}}
    a = r.advisor
    first = message(r.input // 2, r.output // 2, r.cache_read // 2, r.write_5m // 2, r.write_1h // 2)
    second = message(r.input - r.input // 2, r.output - r.output // 2, r.cache_read - r.cache_read // 2,
                     r.write_5m - r.write_5m // 2, r.write_1h - r.write_1h // 2)
    advisor = message(a.input, a.output, a.cache_read, a.write_5m, a.write_1h)
    advisor.update(type="advisor_message", model=a.model)
    return [first, advisor, second]


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
        if r.speed:
            usage["speed"] = r.speed
        if r.inference_geo:
            usage["inference_geo"] = r.inference_geo
        if r.advisor:
            usage["iterations"] = _iterations(r)
        rec = {
            "type": "assistant", "uuid": f"{r.message_id}-u{i}", "timestamp": r.ts,
            "sessionId": r.session_id or session_id,
            "message": {"id": r.message_id, "model": r.model, "role": "assistant",
                        "content": [{"type": "text", "text": r.text}] if i == 0 and r.text else [],
                        "usage": usage},
        }
        if r.entrypoint:
            rec["entrypoint"] = r.entrypoint
        if r.git_branch:
            rec["gitBranch"] = r.git_branch
        if r.effort:
            rec["effort"] = r.effort
        if i == 0:
            rec["message"]["content"] += [
                {"type": "tool_use", "id": f"t{n}", "name": t, "input": {}}
                for n, t in enumerate(r.tools)]
        if i == r.lines - 1:
            rec["message"]["content"] += [
                {"type": "tool_use", "id": f"s{n}", "name": "Skill", "input": {"skill": s}}
                for n, s in enumerate(r.skills)]
            rec["message"]["content"] += [
                {"type": "tool_use", "id": f"c{n}", "name": name, "input": inp}
                for n, (name, inp) in enumerate(r.tool_calls)]
        if r.forked_from:
            rec["forkedFrom"] = {"sessionId": r.forked_from, "messageUuid": f"{r.message_id}-u{i}"}
        out.append(rec)
    return out


def compact_line(uuid: str, ts: str, pre: int, post: int, trigger: str = "manual") -> str:
    """A `compact_boundary` system record, for `write_transcript(extra_lines=[...])`."""
    return json.dumps({"type": "system", "subtype": "compact_boundary", "uuid": uuid,
                       "timestamp": ts, "parentUuid": None,
                       "compactMetadata": {"trigger": trigger, "preTokens": pre,
                                           "postTokens": post}})


def _user(uuid: str, ts: str, content, session_id: str | None = None, **extra) -> str:
    rec = {"type": "user", "uuid": uuid, "timestamp": ts,
           "message": {"role": "user", "content": content}, **extra}
    if session_id:
        rec["sessionId"] = session_id
    return json.dumps(rec)


def prompt_line(uuid: str, ts: str, text: str = "do the thing",
                session_id: str | None = None) -> str:
    """A plain-text prompt the user typed, for `write_transcript(extra_lines=[...])`."""
    return _user(uuid, ts, text, session_id, origin={"kind": "human"})


def slash_command_line(uuid: str, ts: str, name: str = "/review") -> str:
    """A slash command the user typed (stored wrapped in `<command-name>`)."""
    return _user(uuid, ts, f"<command-message>{name[1:]}</command-message>\n"
                           f"<command-name>{name}</command-name>")


def tool_result_line(uuid: str, ts: str) -> str:
    """A user record that carries a tool result: not a prompt."""
    return _user(uuid, ts, [{"type": "tool_result", "tool_use_id": "t0", "content": "ok"}])


def block_prompt_line(uuid: str, ts: str, text: str = "look at this", image: bool = True) -> str:
    """A prompt whose content is a list of blocks: text, plus an image when `image`."""
    blocks = [{"type": "text", "text": text}]
    if image:
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}})
    return _user(uuid, ts, blocks, origin={"kind": "human"})


def compact_summary_line(uuid: str, ts: str, parent_uuid: str | None = None,
                         text: str = "This session is being continued from a previous conversation...") -> str:
    """The user record holding a compaction summary: not a prompt. `parent_uuid` is the uuid of
    the compact boundary it follows."""
    return _user(uuid, ts, text, isCompactSummary=True, parentUuid=parent_uuid)


def queued_prompt_line(uuid: str, ts: str, text: str = "also do this") -> str:
    """A message the user typed while Claude was working (a `queued_command` attachment)."""
    return json.dumps({"type": "attachment", "uuid": uuid, "timestamp": ts,
                       "attachment": {"type": "queued_command", "prompt": text,
                                      "origin": {"kind": "human"}}})


def hook_injection_line(uuid: str, ts: str) -> str:
    """A hook / system injection stored as a user record: not a prompt."""
    return _user(uuid, ts, "<system-reminder>hook output</system-reminder>")


def skill_listing_line(ts: str, skills: dict[str, str], uuid: str = "sl") -> str:
    """A `skill_listing` attachment; `skills` maps skill name -> description."""
    content = "\n".join(f"- {n}: {d}" for n, d in skills.items())
    return json.dumps({"type": "attachment", "uuid": f"{uuid}-{ts}", "timestamp": ts,
                       "attachment": {"type": "skill_listing", "content": content,
                                      "skillCount": len(skills), "names": list(skills),
                                      "isInitial": True}})


def mcp_listing_line(ts: str, servers: dict[str, str], uuid: str = "ml",
                     removed: tuple[str, ...] = ()) -> str:
    """An `mcp_instructions_delta` attachment; `servers` maps display name -> instructions."""
    return json.dumps({"type": "attachment", "uuid": f"{uuid}-{ts}", "timestamp": ts,
                       "attachment": {"type": "mcp_instructions_delta",
                                      "addedNames": list(servers),
                                      "addedBlocks": [f"## {n}\n{t}" for n, t in servers.items()],
                                      "removedNames": list(removed)}})


def tool_use_line(ts: str, tool_use_id: str, name: str, input: dict | None = None,
                  message_id: str = "tu") -> str:
    """An assistant record holding one `tool_use` block (no usage: not a Request)."""
    return json.dumps({"type": "assistant", "uuid": f"u-{tool_use_id}", "timestamp": ts,
                       "message": {"id": f"{message_id}-{tool_use_id}", "role": "assistant",
                                   "content": [{"type": "tool_use", "id": tool_use_id,
                                                "name": name, "input": input or {}}]}})


class DataRoot:
    """A synthetic ~/.claude. Always a path under pytest's tmp_path, never the real one."""

    def __init__(self, path: Path):
        self.path = path
        (path / "projects").mkdir(parents=True, exist_ok=True)

    def write_transcript(self, project: str, session_id: str, reqs: list[Req],
                         extra_lines: list[str] | None = None, title: str | None = None,
                         branch: str | None = None, cwd: str | None = None,
                         prompt: str | None = None) -> Path:
        """`title`/`branch`/`cwd`/`prompt` add the records Spotlights read: a custom-title
        record, and a first user record carrying gitBranch, cwd and the first prompt."""
        d = self.path / "projects" / project
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{session_id}.jsonl"
        head = []
        if title:
            head.append(json.dumps({"type": "custom-title", "customTitle": title,
                                    "sessionId": session_id}))
        if branch or cwd or prompt:
            rec = {"type": "user", "sessionId": session_id, "timestamp": "2026-10-01T00:00:00Z",
                   "message": {"role": "user", "content": prompt or "<system-reminder>x"}}
            if branch:
                rec["gitBranch"] = branch
            if cwd:
                rec["cwd"] = cwd
            head.append(json.dumps(rec))
        lines = head + [json.dumps(rec) for r in reqs for rec in assistant_lines(r, session_id)]
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
                                  reqs: list[Req], meta: dict | None = None,
                                  extra_lines: list[str] | None = None) -> Path:
        """`meta` is written to the sibling agent-<id>.meta.json (agentType, toolUseId, ...)."""
        d = self.path / "projects" / project / session_id / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"agent-{agent_id}.jsonl"
        if meta is not None:
            (d / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta))
        f.write_text("\n".join([json.dumps(rec) for r in reqs
                                for rec in assistant_lines(r, session_id)] + (extra_lines or [])) + "\n")
        return f


class DesktopStore:
    """A synthetic Desktop session store directory (claude-code-sessions/<acct>/<org>/)."""

    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True, exist_ok=True)

    def write_session(self, desktop_id: str, cli_session_id: str | None = None, **fields) -> Path:
        """Writes local_<desktop_id>.json; fields: priorCliSessionIds, spawnedFrom,
        forkedFromSessionId (both refer to other `local_<id>` Desktop ids)."""
        rec = {"sessionId": f"local_{desktop_id}", **fields}
        if cli_session_id:
            rec["cliSessionId"] = cli_session_id
        f = self.path / f"local_{desktop_id}.json"
        f.write_text(json.dumps(rec))
        return f
