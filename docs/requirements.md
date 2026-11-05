# claude-cost v1 spec

## Problem Statement

I use Claude Code heavily from both the CLI and the Desktop app, and I can't see where the money goes. Usage pages show totals, not causes. A one-off analysis showed that a few patterns dominate spend: cache writes (especially cold resumes, where a 100K+ context goes idle past the 1h cache TTL and is written again at the 1h write price), Fable usage, subagents that start with a wide context, skill and MCP listings that are loaded but never used, and sessions that keep growing far past the point where compacting would have paid off. That analysis was a pile of throwaway scripts, it double-counted spend that appears in several transcripts (forks, resumes, `/clear` copies, worktree moves), and its savings estimates were too optimistic.

I need a repeatable tool that tells me, from my own local data and without using AI, what my workflow costs, how that changes over time, which patterns are draining money, and what a given piece of work (such as one ticket from start to merge) cost in total.

## Solution

`claude-cost` is a local tool with two interfaces over one local store:

- A **web server** bound to localhost, with a Dashboard (prepared views for rolling 7 days, calendar weeks and calendar months, each compared with earlier periods), a Comparison view for any set of periods, an Explore view with every filter and group-by, a session timeline, Spotlight pages, and a findings view of Drains and Opportunities.
- A **CLI** for quick answers in the terminal (today, this week, drains, ingest, prices, config) and to start the server.

It reads transcripts from one or more **Data roots**, ingests them step by step into a local SQLite store that keeps history even after Claude Code deletes old transcripts, counts every **Request** exactly once (ADR-0001), prices it at API list price from a dated price table, and links **Sessions**, **Subagents**, **Side sessions** and **Forks** so that totals roll up correctly. Every finding shows **Attributed spend** (measured) and **Avoidable spend** (a conservative lower bound, and for some findings an upper bound too, with its formula), and avoidable figures are never summed across findings.

All terms in this spec are defined in `GLOSSARY.md`.

## User Stories

### Sources and ingestion

1. As a user, I want the tool to read `~/.claude` by default, so that it works with no setup.
2. As a user, I want to add more Data roots (other Claude config folders), so that I can analyse several profiles together or separately.
3. As a user, I want each Data root to optionally include the Desktop session store, so that side sessions and forks started from Desktop are linked to their parent sessions.
4. As a user, I want to filter by Surface (CLI, Desktop, SDK), so that I can see what each client costs.
5. As a user, I want to filter by Data root, so that I can compare profiles.
6. As a user, I want ingestion to be incremental, so that refreshing takes well under a second after the first run.
7. As a user, I want `claude-cost ingest` to run ingestion explicitly, so that I can refresh from the terminal or a script.
8. As a user, I want a full rebuild option, so that I can re-ingest everything after a rule or price change.
9. As a user, I want ingested requests kept after their transcripts are deleted, so that monthly comparisons still work for older months.
10. As a user, I want a warning on first run if `cleanupPeriodDays` is below 60, so that I know older history will be lost from the transcripts.
11. As a user, I want transcripts that were appended to since the last ingest to add only their new requests, so that totals never double.

### Counting and linking

12. As a user, I want every Request counted exactly once even when it appears in several transcripts, so that totals aren't inflated.
13. As a user, I want copied history in a Fork to stay owned by the original Session, so that the Fork shows only its own spend.
14. As a user, I want resume and `/clear` copies attributed to the Session that really made the request, so that old work isn't credited to the new transcript.
15. As a user, I want transcripts copied by a worktree move counted once, so that moving a worktree doesn't double its spend.
16. As a user, I want a Session to be the logical conversation even when it spans several transcripts, so that its cost reflects the whole conversation.
17. As a user, I want Subagent spend shown on its own and rolled up into its parent Session, so that I can see both the child cost and the full session cost.
18. As a user, I want Side sessions linked to the Session that started them, so that I can follow work that branched off.
19. As a user, I want sessions with no link information shown as top-level sessions, so that nothing is lost when Desktop metadata is missing.

### Pricing

20. As a user, I want Cost to be the API list-price equivalent, so that I can compare patterns in dollars even on a subscription.
21. As a user, I want the price table dated by model, so that a price change (such as Fable 5 to 5.1) prices old and new requests correctly.
22. As a user, I want Haiku 5.5 requests over 100K tokens priced at the higher tier, so that long Haiku calls aren't undercounted.
23. As a user, I want fast-mode requests priced at their premium, with cache multipliers on top, so that fast mode shows its real cost.
24. As a user, I want US-only inference priced at 1.1×, so that regional pricing is reflected when it appears.
25. As a user, I want each Cost component (input, output, cache write 5m, cache write 1h, cache read) priced separately, so that I can see what drives each request's cost.
26. As a user, I want requests with an unknown model flagged rather than silently priced, so that I know when the price table is out of date.
27. As a user, I want `claude-cost prices` to list the table in effect and any unpriced models found, so that I can check pricing quickly.
28. As a user, I want to override prices in config, so that I can correct the table without waiting for a release.

### Periods and comparison

29. As a user, I want a `last7` mode comparing the last 7 days with the 7 days before, so that I see this week's trend at a glance.
30. As a user, I want an "Include today" checkbox, so that I can choose between a partial today and 7 complete days.
31. As a user, I want a calendar-week mode comparing with the previous week, so that I can track week-on-week change.
32. As a user, I want a calendar-month mode comparing with the previous month, so that I can track month-on-month change.
33. As a user, I want weeks to start on Monday and days to follow my local time zone, so that periods match my calendar.
34. As a user, I want a partial current period compared with the same number of elapsed days in the previous period, so that comparisons are fair mid-week or mid-month.
35. As a user, I want partial periods clearly marked, so that I don't misread an incomplete total.
36. As a user, I want to pick two or more weeks or months, not necessarily next to each other, so that I can compare any periods I care about.
37. As a user, I want to choose which selected period is the Baseline, so that deltas are measured against the period I mean.
38. As a user, I want compared periods overlaid by day of period, so that I can see where in the week or month spend differs.
39. As a user, I want a table with each period's total, Δ$ and Δ% vs the Baseline, so that I can read the change precisely.

### Dashboard

40. As a user, I want a Dashboard with daily spend for the current period mode, so that I see the trend on the first page.
41. As a user, I want the Dashboard to show the cost split by Cost component, so that I see how much goes to cache writes vs reads vs output.
42. As a user, I want a cache card with hit ratio, cold vs warm write spend, 5m vs 1h write spend and rebuild count, so that cache health is visible immediately.
43. As a user, I want the cache card to show deltas against the comparison period, so that I see whether cache behaviour improved.
44. As a user, I want top-N cards for Spotlights, sessions, projects and findings, so that the biggest items are one click away.
45. As a user, I want N to default to 10 and be configurable, so that I can see more or less.
46. As a user, I want unpriced models flagged on the Dashboard, so that I notice gaps in pricing.

### Explore

47. As a user, I want to filter and group by date, Data root, Surface, project, git branch, Session, Subagent type, model, effort, speed, Cost component, cache event, tool, skill, Drain and Opportunity, so that I can slice spend any way I need.
48. As a user, I want to add, change and remove filters freely, so that I can explore without prepared views.
49. As a user, I want Explore to show a chart and a table for the current slice, so that I can see both shape and numbers.
50. As a user, I want filters stored in the URL, so that I can bookmark and share a view with myself.
51. As a user, I want CSV export of the current table, so that I can analyse further elsewhere.
52. As a user, I want to drill down from any row to Sessions and from a Session to its requests, so that I can find the cause behind a number.

### Session timeline

53. As a user, I want a timeline per Session showing context size and cost per request, so that I can see how a session's cost built up.
54. As a user, I want idle gaps and Cold resumes marked on the timeline, so that I can see when a resume rebuilt the cache.
55. As a user, I want compactions marked, with Late compacts highlighted, so that I can see when compacting came too late.
56. As a user, I want Subagent launches and Side sessions marked on the timeline, so that I can see where work branched and what it cost.
57. As a user, I want the timeline to show the Compact threshold for the session's model, so that I can see when the session crossed it.

### Spotlights

58. As a user, I want to define a Spotlight by a rule (a pattern over title, git branch, worktree path and first prompt), so that sessions for one piece of work are grouped automatically.
59. As a user, I want to pin and unpin sessions in a Spotlight, so that I can fix what the rule gets wrong.
60. As a user, I want Auto-spotlights created for every key a configured pattern (such as `PROJ-\d+`) finds, so that each ticket gets a Spotlight with no setup.
61. As a user, I want key detection to prefer custom title, then git branch, then worktree path, then first prompt, and collect every key per session, so that tickets are found reliably.
62. As a user, I want a Spotlight's total to include its sessions' Subagents and Side sessions, so that I see the full cost of the work.
63. As a user, I want Forks included only when they match the rule or are pinned, and otherwise listed as "related, excluded", so that unrelated follow-up work doesn't inflate a ticket.
64. As a user, I want a session in two Spotlights counted fully in both and marked "also in", so that numbers aren't invented by splitting.
65. As a user, I want Spotlight totals never added together, so that overlaps don't double-count.
66. As a user, I want the Dashboard's top Spotlights ranked by spend inside the period, with lifetime total and first and last activity alongside, so that I see both recent and overall cost.
67. As a user, I want a Spotlight page with lifetime cost per day broken down by session, Subagent and Cost component, so that I can explain what a ticket cost.
68. As a user, I want to compare several Spotlights side by side, so that I can see which tickets were expensive and why.
69. As a user, I want pinned Spotlights stored locally and editable in the web UI, so that I don't edit config files for them.

### Findings

70. As a user, I want each finding to show Attributed spend and Avoidable spend separately, so that measured facts and estimates aren't mixed.
71. As a user, I want Avoidable spend to always have a conservative lower bound, with its formula on the finding's page, and for some findings an upper bound that counts effects the lower bound leaves out, so that forecasts are never optimistic and findings rank by the lower bound (ADR-0006).
72. As a user, I want a negative avoidable estimate shown as $0, so that a finding never claims a saving that didn't exist.
73. As a user, I want avoidable figures labelled "not additive" and no "total avoidable" headline, so that overlapping findings don't overstate savings.
74. As a user, I want Drains and Opportunities shown separately, so that real waste isn't confused with choices I may have made on purpose.
75. As a user, I want a Cold resume Drain (idle at least 60 minutes and context at least 100K), with avoidable spend = the cold write minus the cost of a fresh session start, so that I see what resuming instead of starting fresh cost me.
76. As a user, I want an Oversized context Drain for requests above the Compact threshold, with avoidable spend = reads above the threshold minus the cost of compacting there, so that I see what not compacting cost.
77. As a user, I want the Compact threshold learned per model from my own history, so that it reflects my sessions and each model's read price.
78. As a user, I want the threshold to fall back to a global value when a model has fewer than 20 compactions, so that thin data doesn't produce a wild number.
79. As a user, I want to see the cost-vs-threshold curve and the payback in requests, so that I can judge how reliable the threshold is.
80. As a user, I want the compaction cost in that model to use a pessimistic (p75) estimate, so that unmeasurable rework after compacting doesn't make savings look better.
81. As a user, I want an Unused listing Drain for skills and MCP servers listed in a project's sessions but never invoked there in the period, with avoidable spend a range: the read cost of their Listings as the lower bound, plus the write that stored them, Cold writes that rebuilt them and any Prompt tier drop as the upper bound (ADR-0006), so that I know what to disable per project.
82. As a user, I want "unused" judged per project, so that a skill used daily in one project isn't flagged because another project never uses it.
83. As a user, I want a Fable usage Opportunity, with avoidable spend = Fable cost minus the same tokens at Opus 5.5 prices, so that I can decide when Fable is worth it.
84. As a user, I want a Wide subagent Opportunity for Subagents whose first request has at least 50K context, so that I can spot expensive delegations.
85. As a user, I want a Fork subagent Opportunity, so that I can review forks without them being called waste.
86. As a user, I want every threshold configurable, so that I can tune findings to my workflow.
87. As a user, I want to drill from a finding to the sessions and requests it covers, so that I can see concrete examples.

### CLI

88. As a user, I want `claude-cost serve` to ingest, start the server on localhost and open the browser, so that one command gets me to the Dashboard.
89. As a user, I want `claude-cost today` and `claude-cost week` to print spend tables with comparison deltas, so that I can check spend without a browser.
90. As a user, I want `claude-cost drains [--since]` to list findings with attributed and avoidable spend, so that I can review them in the terminal.
91. As a user, I want `claude-cost config` to print every value in effect, with its default and where it came from, so that I know what the tool is using.
92. As a user, I want `claude-cost config get` and `config set` with validation of key and type, so that I can change one value safely.
93. As a user, I want `claude-cost config edit` and `config path`, so that I can edit the file directly when I prefer.
94. As a user, I want the config file created with commented defaults on first run, so that I can discover the options.

### Freshness, privacy and install

95. As a user, I want the server to ingest on start and on page load when data is older than 60 seconds, with a Refresh button, so that pages are current without a file watcher.
96. As a user, I want the server bound to 127.0.0.1 only, so that my prompts and titles never leave the machine.
97. As a user, I want the tool to make no network calls and use no AI, so that it is private and free to run.
98. As another developer, I want to install the tool with `uv tool install` from the GitHub repo, so that I can use it on my own data.

## Implementation Decisions

- **Language and stack:** Python managed with `uv`; SQLite as the local store; a small web framework with server-rendered pages and one charting library; no Node build step. Install via `uv tool install` from the repo.
- **Modules:**
  - **Sources:** discovers transcripts, subagent metadata and Desktop session metadata under each Data root; knows the on-disk shapes.
  - **Ingest:** incremental ingestion tracked per file by size and offset; parses requests, compactions, Listings, tool and skill use, link records; writes to the store. Supports full rebuild.
  - **Ownership and linking:** applies ADR-0001 (one owner per request, by message id; owner is the transcript whose filename equals the record's `sessionId`, else the earliest), groups transcripts into Sessions, and links Subagents, Side sessions and Forks.
  - **Pricing:** a dated price table keyed by model and effective date, with prompt-size tiers (Haiku 5.5 over 100K), speed (fast mode), inference region (US 1.1×), and per-model cache-read multipliers; config overrides; unknown models flagged.
  - **Findings:** computes each Drain and Opportunity with Attributed spend, Avoidable spend and its formula; learns the Compact threshold per model.
  - **Spotlights:** rules, pins, Auto-spotlight patterns, closure over Subagents and Side sessions, Fork handling, overlap marking.
  - **Query service:** the one interface used by both the web server and the CLI: totals, breakdowns by any dimension, periods and comparisons, session timelines, Spotlights, findings.
  - **Web server** and **CLI:** thin adapters over the query service.
  - **Config:** a TOML file in the user config folder, created with commented defaults; holds Data roots, thresholds, Auto-spotlight patterns, price overrides, top N and port. Pinned Spotlights live in the store.
- **Facts the parser relies on:**
  - Requests carry `usage` with the 5m/1h cache-write split (present on all recent requests), `speed` and `inference_geo`.
  - Transcripts that are resume copies hold lines whose `sessionId` differs from the filename; worktree moves leave a `relocated` record.
  - Subagent transcripts have a sibling metadata file whose `toolUseId` matches the parent's tool call and which records agent type and fork status.
  - Desktop session metadata records `cliSessionId`, `spawnedFrom` (Side sessions), `forkedFromSessionId` and `priorCliSessionIds`.
  - Compactions are `system` records with subtype `compact_boundary` and `compactMetadata` (`trigger`, `preTokens`, `postTokens`).
  - Listings come from `skill_listing` (per-skill names and lines) and `mcp_instructions_delta` (per-server blocks) attachments; deferred-tool deltas list tool names grouped by server.
- **Cache event classification:** each cache write is Cold (rebuilding context the session already had, after the cache expired) or Warm (new context); cache reads are a third event type. Hit ratio = cache read / (cache read + cache write + uncached input).
- **Compact threshold model:** for each candidate threshold T and each session that crossed T, compare actual cost with a counterfactual of compacting at T: compaction cost (summary output plus writing `postTokens`, at p75 of observed compactions) plus later requests reading from `postTokens` plus observed growth. T minimises total cost per model; fall back to a global T below 20 compactions for a model. Expose the cost-vs-T curve and payback in requests.
- **Avoidable spend formulas:**
  - Cold resume = cold write cost minus the cost of the user's median first-request context written fresh.
  - Oversized context = reads above T minus the cost of compacting at T.
  - Unused listing = read cost of the unused Listings (lower bound); the upper bound adds their write, Cold-write rebuilds and any Prompt tier drop (ADR-0006).
  - Fable usage = Fable cost minus the same tokens at Opus 5.5 prices.
  - Wide subagent and Fork subagent: Attributed spend only.
  - Negative results are shown as $0. Avoidable figures are not additive across findings.
- **Periods:** local time zone; weeks start Monday; modes `last7` (with Include today), calendar week, calendar month; partial periods compared on equal elapsed days; Comparison only between periods of one mode, with a user-chosen Baseline (default: earliest selected).
- **Retention:** the store never deletes rows when transcripts disappear.
- **Privacy:** bind to 127.0.0.1 only; no outbound network calls; no AI calls.

## Testing Decisions

- **What makes a good test:** tests drive the tool through its public interfaces and check results a user would see (totals, rows, deltas, finding figures), never internal tables or helper functions.
- **Seam 1 (main): the query service.** Each test builds a fixture Data root of small synthetic transcripts (and, where needed, Desktop session metadata), runs ingest, and asserts through the query service. Cases:
  - one fixture per duplication case: Fork, worktree relocation, resume copy, unmarked shared uuids, Side session, Subagent; each asserts request count and Cost
  - pricing: Haiku 5.5 above and below 100K, fast mode with cache multipliers, US region, dated price change, unknown model flagged
  - periods: `last7` with and without today, week and month edges, partial-period comparison, arbitrary multi-period comparison with a chosen Baseline
  - findings: each Drain and Opportunity on a fixture built to trigger it, including the learned Compact threshold and the global fallback; negative avoidable shown as $0
  - Spotlights: rule, pin, Auto-spotlight, closure over Subagents and Side sessions, Fork exclusion, overlap
  - incremental ingest: append to a transcript, ingest again, totals change by exactly the new requests
- **Seam 2: adapter smoke tests.** An HTTP test client loads every page and the CSV export on one fixture root; each CLI command runs against the same root and its output shape is checked. No logic is asserted here.
- **Seam 3: parity on real data.** A manual check that new totals for a fixed window match the original analysis scripts within a stated tolerance, adjusted for the known fixes (ownership rule, Sonnet 5.5 cache-read price, Haiku 5.5 tier). It gates deleting the old scripts and is not part of the normal test run.
- **Prior art:** none in the repo yet; the original analysis scripts serve as the reference for expected totals.

## Out of Scope

- Desktop Cowork / agent-mode sessions (a different record format), planned for later.
- Any use of AI to classify or summarise sessions.
- A joint "total avoidable" estimate across overlapping findings.
- Saved Explore views beyond URL bookmarks.
- A file watcher or live push updates.
- Remote or multi-user hosting.
- Subscription-share pricing (cost is API list-price equivalent only).
- The stale-cache guard hook: it stays in the repo but is not part of v1.
- Budget alerts or notifications.

## Further Notes

- Request ownership is recorded in ADR-0001. Changing the rule moves spend between sessions and requires a full re-ingest.
- The compaction data is thin: 48 compactions, all manual, none automatic. The Compact threshold should be read with its curve and sample size until more data exists.
- The original analysis scripts priced Sonnet 5.5 cache reads at $0.20 instead of $0.10 and priced Haiku 5.5 flat; parity checks must account for both fixes.
- Splitting this spec into per-feature implementation issues is a separate step.
