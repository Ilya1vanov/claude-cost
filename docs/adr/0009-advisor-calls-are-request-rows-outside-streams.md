# An Advisor call is its own Request row, with a derived id, outside every Stream

A Request that uses the advisor tool records the advisor's own billed call only as an `advisor_message` entry in `usage.iterations`, at the advisor's model, which can differ from the executor's. The top-level usage already sums the executor's `message` iterations, so only the non-`message` iterations are new spend. Each is stored as its own row in `requests` with the id `<message_id>#advisor-<n>`, owned like its parent and priced at its own model, so totals, breakdowns and per-model splits pick it up unchanged. It has no prompt cache and does not grow the context, so it is marked as an Advisor call and everything Stream-based skips it: cache events, Stream facts, the Compact analysis and every finding detector.

## Considered Options

- **Extra cost columns on the parent row**: totals stay right, but the advisor's cost lands under the executor's model and breaks per-model breakdowns.
- **A separate advisor table**: Streams stay clean with no filter, but every spend query has to add it in, and there are far more spend queries than Stream readers.

## Consequences

- A row in `requests` is not always a turn of the conversation: code that walks a Stream must filter Advisor calls out, or it will see a large uncached request with a different model.
- Its owner follows its parent's whenever ownership is recomputed; it has no copies of its own.
- Stores ingested before this change hold no Advisor calls, so the change needs a full re-ingest, done by the store migration.
