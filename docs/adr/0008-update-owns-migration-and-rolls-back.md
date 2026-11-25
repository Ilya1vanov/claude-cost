# An update migrates the store in one transaction and rolls back as a whole

A new version may change the store, which holds Spotlights that cannot be rebuilt from transcripts. `claude-cost update` stops a running dashboard, backs up the store, installs the new wheel, and runs the new version's migration: the schema change and any re-ingest happen in a single SQLite transaction. If it fails, the store is left untouched and `update` reinstalls the previous wheel, so an update either completes or leaves the user exactly where they were.

## Consequences

- Every launch still checks the store's schema version and migrates in the same single transaction, so an update made outside `claude-cost update` never leaves a half-migrated store; that path cannot restore the previous version, so the new version refuses to run and names the command that does.
- An older version refuses a store with a newer schema instead of reading it.
- The backup (the most recent one only, beside the store) is for a manual downgrade after a successful but buggy update.
