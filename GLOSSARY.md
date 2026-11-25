# claude-cost

Measures what Claude Code usage costs, from local transcripts, to find the parts of a workflow that cost the most.

## Sources

**Data root**:
A Claude config folder whose transcripts are read, such as `~/.claude`, plus the optional Desktop session store that links sessions to each other.
_Avoid_: claude folder, profile

**Project**:
The working directory Claude Code groups a session's transcripts under; a worktree is a project of its own.
_Avoid_: repo, folder, workspace

**Surface**:
The client that ran a session: CLI, Desktop, SDK.
_Avoid_: app, interface, entrypoint

**Transcript**:
One file Claude Code writes a session's records to; a session spans one or more through resumes and clears, and a request may have copies in several.
_Avoid_: stem, log, file

## Units of spend

**Request**:
One billed API call, counted once no matter how many transcripts contain a copy of it.
_Avoid_: message, call

**Advisor call**:
A Request the advisor tool makes while answering another Request, priced at its own model and owned like that Request; it is not part of any Stream.
_Avoid_: advisor message, iteration, sub-request

**Turn**:
One prompt the user typed into a session, with the requests made to answer it.
_Avoid_: request, exchange, message

**Session**:
One logical conversation, which may span several transcripts through resumes and clears, owning the requests it made itself.
_Avoid_: conversation, chat, transcript

**Parent session**:
The session a side session or fork was started from.
_Avoid_: origin, source session

**Side session**:
A separate session started from a parent session, which stays linked to it.
_Avoid_: child session, spawned session

**Fork**:
A session started from a parent session with a copy of its history; the copied requests stay owned by the parent session.
_Avoid_: branch, clone

**Subagent**:
A child run launched from inside a session, whose spend rolls up into that session.
_Avoid_: agent, task, sidechain

**Fork subagent**:
A subagent started with a copy of its session's history; it stays part of that session and is not a fork.
_Avoid_: forked agent

**Stream**:
A Session's own requests, or one Subagent's requests, in time order; prompt caching, idle gaps and Listings belong to a stream.
_Avoid_: thread, sidechain, transcript

**Subagent type**:
The kind of a subagent, such as Explore or Plan, as named in its transcript metadata; requests made by the session itself have none.
_Avoid_: agent type, agent

**Effort**:
The reasoning effort level a request was made with, such as low, medium or high; older transcripts do not record it.
_Avoid_: thinking level, reasoning budget

**Setting change**:
A request whose model or effort differs from the previous request in its stream; the first request is never one, and a request whose effort is not recorded does not change effort.
_Avoid_: switch, model swap

**Timeline**:
A session's turns, requests and events in time order, read like the conversation they came from, each request with its context size and cost.
_Avoid_: transcript history, chat, history

**Breakdown**:
Spend split by one dimension, such as date, project, model or tool, one row per value.
_Avoid_: group by, explore, pivot

## Money

**Cost**:
The API list-price equivalent of a request, from a dated price table.
_Avoid_: spend estimate, bill

**Cost component**:
One of the priced parts of a request: input, output, cache write (5m), cache write (1h), cache read.
_Avoid_: token type

**Cold write**:
A cache write that rebuilds context the cache already held before it expired.
_Avoid_: cache miss, rebuild, cold read

**Warm write**:
A cache write of context that is new to the session, as the conversation grows.
_Avoid_: incremental write

**Cache loss**:
The Cost paid for cold writes beyond what reading the same tokens from the cache would have cost; it counts every cold write, avoidable or not.
_Avoid_: rebuild overhead, cache tax, miss cost

**Prompt tier**:
A higher rate a model charges for every token of a request whose prompt is above a set size.
_Avoid_: long-context tier, tier

## Spotlights

**Spotlight**:
A named set of sessions the user picks, whose total cost is tracked together, including their subagents, their side sessions, any forks picked too and the members of its included spotlights.
_Avoid_: group, tag, bucket

**Spotlight id**:
The stable identity of a spotlight: generated for a user-defined one, derived from its key (`auto:<KEY>`) for an auto-spotlight. Its name is an editable, non-empty label that need not be unique; a reference is resolved by id, then key, then a name only one spotlight has.
_Avoid_: slug, handle

**Picked session**:
A session the user added to a spotlight by hand, rather than one that joined as a side session or by key.
_Avoid_: pinned session, rule match

**Included spotlight**:
A spotlight whose members count toward another spotlight that includes it, each session once; a spotlight that includes others cannot itself be included.
_Avoid_: child spotlight, sub-spotlight, group

**Auto-spotlight**:
A spotlight created for each key, such as a ticket id, that a configured pattern finds in sessions; it can be renamed, and its key stays the same.
_Avoid_: ticket group

**Key match**:
A session that joins an auto-spotlight because its key appears in the session's custom title, or, when the title carries no key, in its git branch or worktree path.
_Avoid_: rule match, auto member

**Suggested session**:
A session that carries a key only where it may be a mere mention, such as the first prompt, or the branch when the title names another key, or any session carrying the key of a locked auto-spotlight; it is offered to the spotlight for that key but not counted until the user accepts it.
_Avoid_: candidate, weak match, possible member

**Dismissed session**:
A session that carries a spotlight's key but that the user removed or declined; it stays out of that spotlight while it carries the key.
_Avoid_: excluded session, ignored session, unpinned session

**Locked auto-spotlight**:
An auto-spotlight whose membership the user controls: sessions that carry its key are only suggested and join when the user accepts them.
_Avoid_: converted spotlight, frozen spotlight, claimed key

**Dismissed key**:
A key the user hid, so no auto-spotlight is shown for it until the user restores it.
_Avoid_: ignored key, hidden spotlight, muted key

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
An estimate of what would have been saved without the drain or opportunity: a conservative lower bound, and for some findings also an upper bound that counts effects the lower bound leaves out.
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

**Unused listing**:
A Listing never invoked in its project during the Period.
_Avoid_: dead listing, idle skill

**Bare subagent context**:
The first-request context of the user's leanest subagent type: the smallest start a delegation could have had.
_Avoid_: bare agent, baseline context, minimal agent

**Oversized context**:
Requests whose context is above the compact threshold, whether or not the session later compacts.
_Avoid_: overflow, context bloat

**Autocompact window**:
The context size a user sets per model for Claude Code to compact automatically, which fires a fixed amount before that size is reached.
_Avoid_: compact window, compact threshold

**Late compact**:
A compaction, manual or automatic, that came after a request whose context was above the compact threshold.
_Avoid_: late auto-compact

## Releases

**Release**:
A published `vX.Y.Z` version of claude-cost: a tag on `main` with a wheel, plain-language notes taken from the changelog, and a GitHub Release page.
_Avoid_: build, package, version (alone)

**Releases**:
The injected collaborator that returns the latest Release, or none when it has no wheel; tests use a fake and never reach the network.
_Avoid_: release client, updater

**Update check**:
The once-a-day lookup of the latest Release, the only network call claude-cost makes; it sends no usage data or identifiers, caches its answer beside the store, and can be turned off.
_Avoid_: telemetry, phone-home, auto-update

**Installer**:
The injected collaborator that changes the machine: installs or removes the claude-cost tool and runs the new version's migration; tests use a fake and never run `uv`.
_Avoid_: package manager, updater

**Dashboard**:
The web app started by `claude-cost serve`; `update` and `uninstall` stop it, through an injected collaborator, before changing files and restart it if it was running.
_Avoid_: server, web UI

**Schema version**:
The number of migration steps applied to the store, kept in its `PRAGMA user_version`; a store newer than the code is refused.
_Avoid_: db version, store version

**Migration**:
One ordered step taking the store from schema version N-1 to N; all pending steps run in one transaction, so a failure leaves the store unchanged.
_Avoid_: upgrade script, schema change

## Time

**Period**:
A span of time that spend is reported for: a rolling 7, 30 or 90 days, a calendar week, a calendar month, or a custom period.
_Avoid_: range, window

**Custom period**:
A run of whole local days the user picks, both ends included; its baseline is the equally long run of days just before it, and it is never partial.
_Avoid_: date range, arbitrary period

**Concurrency**:
How many Sessions made a request in the same 5-minute slot. Slots are fixed, clock-aligned and local (12:00-12:05, 12:05-12:10...); a Session is active in a slot if it or one of its Subagents made a request in it, so Subagents count toward their Session, while Side sessions and Forks count separately from their Parent session. Average concurrency is taken over slots with at least one active Session, so idle time doesn't lower it; peak concurrency is the busiest slot in the Period.
_Avoid_: parallelism, simultaneous sessions

**Comparison**:
A view of two or more periods of the same kind side by side, against one baseline period.
_Avoid_: diff

**Baseline**:
The period in a comparison that the others are measured against.
_Avoid_: reference period
