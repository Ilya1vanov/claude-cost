# Detectors see only Period Requests plus Stream facts stored at ingest

Findings run on every dashboard Period tab, and loading every Request in history made that cost grow with total history. Some detectors do need earlier history: Cold resume needs each Request's idle gap and the lifetime median first-request context, and Wide subagent needs each Subagent's first-request context. That history is per Stream: a Stream idle for weeks, or a Subagent whose first Request predates the Period, can sit arbitrarily far back. So the findings load selects only Period Requests. Facts a detector needs from before the Period are computed per Stream at ingest, on the same "inputs changed" trigger as cache classification and the Compact analysis, and are read back with each Request. Detectors get no all-history rows, so one cannot quietly depend on full history.

## Considered Options

- **A look-back time window declared by each detector** (the original proposal in #33): any fixed window either misses a long-idle Stream's previous Request, which changes figures, or has to be unbounded.
- **Per-Stream lookups at query time** (the last timestamp before the Period, each Subagent's first Request): this keeps the store unchanged but adds queries on every page load, while ingest already walks every Stream in order.

## Consequences

A new detector that needs history from before the Period must add a stored Stream fact and recompute it at ingest, not read more rows. Lifetime baselines, such as the median first-request context, stay lifetime and do not follow the Period.
