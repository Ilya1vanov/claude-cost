# Each request is owned by exactly one transcript

The same billed request appears in several transcript files: forks copy history, resumes and `/clear` copy the old history with its original `sessionId` and timestamps, worktree moves copy whole files to another project folder, and some copies share uuids with no marker at all. In a 60-day sample, 5,591 of 24,431 requests had more than one copy, and only 267 of those came from forks. We count each request once, by message id, and give it to the transcript whose filename equals the record's `sessionId`; only when no such transcript exists does it go to the transcript that started earliest.

## Considered Options

- **Earliest transcript wins** (the original script's rule): wrong for resume copies, because a copy keeps the original timestamps and so can look older than the session that really made the request.
- **Follow explicit link fields only** (`forkedFrom`, `continued-in`, Desktop metadata): too sparse; most copies carry no link.

## Consequences

Spend moves between sessions when the rule changes, so session, spotlight and project totals are only comparable across runs made with the same rule. Any change to this rule needs a full re-ingest.
