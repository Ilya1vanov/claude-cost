"""Timeline rows: the session's prompts and requests with the text and tool calls read from its
transcripts when the Session view is opened (ADR 0005). Nothing here is stored at ingest.

Rows are a tagged union on `kind` ("prompt", "request"); a new kind is a new dataclass with its
own `kind`, so consumers dispatch on it and never see the old rows change shape.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Iterable


TOOL_LINE_LIMIT = 300  # characters kept of a tool line's argument

# The one argument that says what a known tool call did
MAIN_ARGUMENT = {
    "Read": "file_path", "Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path", "Bash": "command", "Grep": "pattern", "Glob": "pattern",
    "WebFetch": "url", "WebSearch": "query", "Agent": "description", "Task": "description",
    "Skill": "skill",
}


@dataclass(frozen=True)
class ToolLine:
    name: str
    summary: str  # "" for an unknown tool or a missing argument


@dataclass(frozen=True)
class PromptRow:
    ts: float
    text: str
    kind: str = "prompt"


@dataclass(frozen=True)
class RequestRow:
    ts: float
    message_id: str
    model: str
    effort: str  # "" when not recorded
    text: str  # assistant text blocks, "" when none or the transcript is gone
    tools: tuple[ToolLine, ...]
    context_tokens: int
    cost: float
    gap_seconds: float | None
    idle: bool  # an idle gap came before it
    cold_write: bool
    cold_resume: bool
    oversized: bool  # context above the Compact threshold of its model
    content_available: bool  # False: "transcript no longer on disk"
    kind: str = "request"


@dataclass(frozen=True)
class Content:
    text: str
    tools: tuple[ToolLine, ...]


def tool_line(block: dict) -> ToolLine:
    name = str(block.get("name") or "")
    inp = block.get("input")
    arg = MAIN_ARGUMENT.get(name)
    value = inp.get(arg) if arg and isinstance(inp, dict) else None
    summary = value.strip().splitlines()[0][:TOOL_LINE_LIMIT] if isinstance(value, str) \
        and value.strip() else ""
    return ToolLine(name, summary)


def scan(path: str, wanted: set[str], prompts: bool
         ) -> tuple[dict[str, Content], list[tuple[str, float, str]]]:
    """One pass over a transcript: the content of the `wanted` message ids (the text and tool
    blocks of every streamed line of a request, in order) and, when `prompts`, its typed prompts."""
    from .ingest import typed_prompt  # ingest imports the query service, which imports this module
    texts: dict[str, list[str]] = {}
    tools: dict[str, list[ToolLine]] = {}
    found: list[tuple[str, float, str]] = []
    with open(path, "rb") as fh:
        for raw in fh:
            try:
                rec = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if prompts and "forkedFrom" not in rec:
                prompt = typed_prompt(rec)
                if prompt:
                    found.append(prompt)
                    continue
            msg = rec.get("message")
            mid = msg.get("id") if isinstance(msg, dict) else None
            if rec.get("type") != "assistant" or mid not in wanted:
                continue
            texts.setdefault(mid, [])
            tools.setdefault(mid, [])
            for block in msg.get("content") if isinstance(msg.get("content"), list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str) \
                        and block["text"].strip():
                    texts[mid].append(block["text"])
                elif block.get("type") == "tool_use":
                    tools[mid].append(tool_line(block))
    return {m: Content("\n\n".join(texts[m]), tuple(tools[m])) for m in texts}, found


def read_content(copies: dict[str, list[str]], main_paths: Iterable[str] = ()
                 ) -> tuple[dict[str, Content], list[PromptRow]]:
    """`copies`: message id -> transcript paths holding it, best copy first. Returns the content
    of every request whose first existing copy has it, and (from `main_paths`) the typed prompts
    of the Session's own transcripts. Each transcript is read once."""
    chosen: dict[str, str] = {}
    for mid, paths in copies.items():
        path = next((p for p in paths if os.path.exists(p)), None)
        if path:
            chosen[mid] = path
    by_path: dict[str, set[str]] = {}
    for mid, path in chosen.items():
        by_path.setdefault(path, set()).add(mid)
    prompt_paths = [p for p in dict.fromkeys(main_paths) if os.path.exists(p)]
    content: dict[str, Content] = {}
    prompts: dict[str, PromptRow] = {}  # by uuid: a resumed transcript may repeat a prompt
    for path in dict.fromkeys([*by_path, *prompt_paths]):
        found, typed = scan(path, by_path.get(path, set()), path in prompt_paths)
        content.update({m: c for m, c in found.items() if chosen.get(m) == path})
        for uuid, ts, text in typed:
            prompts.setdefault(uuid, PromptRow(ts, text))
    return content, list(prompts.values())
