# claude-cost

Measures what Claude Code usage costs, from local transcripts, to find the parts of a workflow that cost the most.

## Sources

**Data root**:
A Claude config folder whose transcripts are read, such as `~/.claude`, plus the optional Desktop session store that links sessions to each other.
_Avoid_: claude folder, profile

**Surface**:
The client that ran a session: CLI, Desktop, SDK.
_Avoid_: app, interface, entrypoint

## Units of spend

**Request**:
One billed API call, counted once no matter how many transcripts contain a copy of it.
_Avoid_: message, turn, call

**Session**:
One logical conversation, which may span several transcripts through resumes and clears, owning the requests it made itself.
_Avoid_: conversation, chat, transcript

**Side session**:
A separate session started from another session, which stays linked to it as a child.
_Avoid_: child session, spawned session

**Fork**:
A session started with a copy of another session's history; the copied requests stay owned by the original session.
_Avoid_: branch, clone

**Subagent**:
A child run launched from inside a session, whose spend rolls up into that session.
_Avoid_: agent, task, sidechain

## Money

**Cost**:
The API list-price equivalent of a request, from a dated price table.
_Avoid_: spend estimate, bill

**Cost component**:
One of the priced parts of a request: input, output, cache write (5m), cache write (1h), cache read.
_Avoid_: token type

**Cold write**:
A cache write that rebuilds context the cache already held before it expired.
_Avoid_: cache miss, rebuild

**Warm write**:
A cache write of context that is new to the session, as the conversation grows.
_Avoid_: incremental write

## Spotlights

**Spotlight**:
A named, saved set of sessions whose total cost is tracked together, including their subagents and side sessions.
_Avoid_: group, tag, bucket

**Auto-spotlight**:
A spotlight created for each key, such as a ticket id, that a configured pattern finds in sessions.
_Avoid_: ticket group

## Findings

**Drain**:
A pattern that wastes money with no benefit, such as a cold resume or an oversized context.
_Avoid_: issue, problem, leak

**Opportunity**:
A costly pattern that may be intentional, shown for review rather than as waste, such as a fork subagent.
_Avoid_: suggestion, warning

**Attributed spend**:
The measured cost of the requests a drain or opportunity covers.
_Avoid_: impact, waste

**Avoidable spend**:
A conservative lower-bound estimate of what would have been saved without the drain or opportunity.
_Avoid_: savings, forecast

**Cold resume**:
A request after a long idle gap whose prompt cache expired, so the whole context is written again.
_Avoid_: stale cache, cache miss

**Compact threshold**:
The context size, derived from the user's own history, above which compacting would have cost less than carrying the context on.
_Avoid_: context limit, compact window

**Listing**:
The text describing a skill or MCP server that is loaded into a session's context whether or not it is used.
_Avoid_: tool definitions, manifest

**Oversized context**:
Requests whose context is above the compact threshold, whether or not the session later compacts.
_Avoid_: overflow, context bloat

**Late compact**:
A compaction, manual or automatic, that started from a context above the compact threshold.
_Avoid_: late auto-compact

## Time

**Period**:
A span of time that spend is reported for: a rolling 7 days, a calendar week, or a calendar month.
_Avoid_: range, window

**Comparison**:
A view of two or more periods of the same kind side by side, against one baseline period.
_Avoid_: diff

**Baseline**:
The period in a comparison that the others are measured against.
_Avoid_: reference period
