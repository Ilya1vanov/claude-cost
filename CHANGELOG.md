# Changelog

What changed in each release of claude-cost, in plain language. Newest first.

<!--
Entry format: a heading `## vX.Y.Z - YYYY-MM-DD` followed by a short paragraph or a few bullets
about what you will notice as a user. Skip internal details. The release step adds the entry for
the version it tags; the release check fails when the tag has no entry.
-->

## v0.3.0 - 2026-11-26

Long sessions are easier to find your way around, and the Parallel sessions card shows how you use Subagents.

- A Timeline map beside a session's Timeline marks where its prompts, compactions, Setting changes, idle gaps, cold writes and Advisor calls fall. Hover a mark to see what it is, click or drag to jump there, and see the Timeline search's matches in their own strip. It also appears on a Subagent's own page.
- The Parallel sessions card shows Subagents per session and the share of Sessions that use Subagents.
- `claude-cost serve` prints the address it serves on even when its output goes to a file or another program.
- The install script tells a used-up GitHub rate limit apart from a network problem.
- Setting up with a coding agent no longer needs python3 or jq to read your settings, and the command that keeps a year of transcripts is no longer skipped.

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
