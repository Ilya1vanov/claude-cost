# Avoidable spend is a range; the lower bound ranks

An Unused listing costs more than its cache reads: the write that first stored it, the Cold writes that rebuild it, and, when it pushes a request over the Prompt tier threshold, the tier surcharge on the whole request. Those effects are real but less certain than the reads, so we give a finding's avoidable spend an optional upper bound beside the conservative lower bound, shown as `$low–$high`, while sorting and ranking keep using the lower bound alone. Unused listing was the first finding with an upper bound, and Oversized context followed (it adds the Cold writes that rebuilt the excess); the others show a single figure until they get one.

## Considered Options

- **One best-guess figure**: simpler to show, but it would mix measured reads with modelled effects and make rankings move whenever the model of those effects changes.
- **Lower bound only (as before)**: understates Unused listing on tiered models, where the tier surcharge can dwarf the reads.

## Consequences

- Wherever avoidable spend shows (Findings page, detail tables, CLI, per-request in the Session view), it can be a range, and the lower bound stays the figure everything is ordered by.
- An upper bound's per-request effects are counted once per request and split across a finding's rows by share, so rows still add up to the finding.
