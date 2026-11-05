# The Timeline reads transcripts on demand

The Timeline shows a session's prompts, assistant text and tool calls, but ingest stores only usage, cost and tool/skill names per request. We read the session's transcripts when its Session view is opened, finding them through the transcript copies already recorded for each request, rather than storing message content at ingest. Storing it would grow the database by roughly the size of every transcript and keep the user's prompts and code in a second place, for a view opened one session at a time.

## Considered Options

- **Store content at ingest**: fast and survives transcript deletion, but a large database holding prompts and code.
- **No content**: the Timeline would be the old Requests table restyled, losing the reason for the view.

## Consequences

- No position is stored per request, so the reader scans each transcript for the requests' message ids.
- Transcript copies are kept after a transcript is deleted, so a request can outlive its text: the Timeline shows its numbers with "transcript no longer on disk" in place of content.
- Stored turns are not linked to requests, so each turn's prompt and the requests that answer it also come from the transcript.
