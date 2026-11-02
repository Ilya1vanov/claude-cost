# A Spotlight is identified by an id, not its name

Spotlights were identified by name: Picked sessions and Included-spotlight edges were stored under it, and an Auto-spotlight's name was its key. Renaming any Spotlight needs a name that can change without breaking what points at the Spotlight. So every Spotlight gets a stable id, and its name becomes an editable, non-empty label that need not be unique. A user-defined Spotlight gets a generated id; an Auto-spotlight's id is derived from its key, so it exists, with its picks, edges and Dismissed sessions, without being stored. Keys stay unique: at most one Auto-spotlight per key.

## Considered Options

- **Keep the name as identity and rewrite every reference on rename**: renaming touches pins, Dismissed sessions and the edges of every Spotlight that includes it, and an Auto-spotlight's name could still never differ from its key.
- **A display name for Auto-spotlights only**: covers renaming Auto-spotlights but not user-defined ones.

## Consequences

Every table keyed by Spotlight name is migrated to ids. The CLI and URLs address a Spotlight by id or key; a name works only while exactly one Spotlight has it, otherwise the command fails and lists the matching ids.
