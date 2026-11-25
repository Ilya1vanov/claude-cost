# Changelog

What changed in each release of claude-cost, in plain language. Newest first.

<!--
Entry format: a heading `## vX.Y.Z - YYYY-MM-DD` followed by a short paragraph or a few bullets
about what you will notice as a user. Skip internal details. The release step adds the entry for
the version it tags; the release check fails when the tag has no entry.
-->

## v0.2.1 - 2026-11-25

Advisor calls and web searches now count towards your spend, and setting up with a coding agent is supported.

- Advisor calls show up as their own entries in a session's timeline and are included in the cost.
- Web searches Claude made are charged to the request that made them, and show as their own cost component.
- Set up claude-cost by pasting one line into your coding agent: it installs claude-cost, keeps a year of Claude Code transcripts, sets your ticket-key pattern, checks the install and offers to start the dashboard. See "Install with your agent" in the README.
- The install script reports the version it just installed and reminds new users to open a new Terminal before running `claude-cost serve`.

## v0.2.0 - 2026-11-25

claude-cost now installs, updates and uninstalls with one command each.

- Install by pasting one line into Terminal; it sets up everything it needs.
- Run `claude-cost update` to move to the newest version. It shows what changed, backs up your data first, and puts the old version back if anything goes wrong.
- Run `claude-cost uninstall` to remove it. You choose whether to keep your Spotlights and settings.
- claude-cost tells you, at most once a day, when a newer version exists. This is the only time it goes online, and you can turn it off in `config.toml` or with `CLAUDE_COST_UPDATE_CHECK=0`.
- Your data is upgraded safely when a new version needs a different layout, and an older version refuses to open data from a newer one.
- Prices now cover Fable 5.2, Mythos 5.2 and Opus 4.5.
