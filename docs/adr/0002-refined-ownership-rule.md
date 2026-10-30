# Ownership prefers originals over flagged copies, and Subagent runs over main-transcript copies

Refines [ADR-0001](0001-request-ownership.md). The filename-equals-`sessionId` rule alone misassigns requests in three cases found in real transcripts, so `ownership.pick_owner` ranks the candidates of a message id by this key, smallest first:

1. **Not a flagged copy.** A transcript that marks the record as copied history (a Fork's `forkedFrom`) loses to one that does not.
2. **Not relocated.** A transcript carrying a `relocated` record (a worktree move copied the whole file) loses to one that does not.
3. **Subagent transcript first.** A Subagent (sidechain) transcript beats a main-transcript copy of the same request: the Subagent run made the request, and the parent only echoes it.
4. **Earliest file, then project and file stem** as deterministic tie-breakers.

Only candidates whose filename stem equals the record's `sessionId` take part when any exist (the ADR-0001 rule); otherwise all candidates do. The order above is applied inside that set.

## Considered Options

- **ADR-0001 as written** (filename match, else earliest): a forked or relocated copy that happens to be named like the session, or a parent echoing a Subagent request, took spend from the transcript that really made it.
- **Earliest transcript first, flags as tie-breakers**: copies keep the original timestamps, so "earliest" is unreliable.

## Consequences

Same as ADR-0001: totals are only comparable across runs made with the same rule, and any change needs `claude-cost ingest --rebuild`. Ingest buckets requests by UTC only to pick the price effective date; day and week reporting uses the configured time zone and is not part of this rule.
