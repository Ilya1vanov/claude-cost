## Agent skills

### Issue tracker

GitHub Issues on this repo, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five roles, label = role name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `GLOSSARY.md` + `docs/adr/`. See `docs/agents/domain.md`.

Glossary and ADR edits are changes too: write them in your worktree under `.claude/worktrees/`, creating one before the first write if the session (e.g. `/grill-with-docs`) started without it.

## Git workflow

Make every change in a worktree under `.claude/worktrees/`, never in the main checkout.

When work is done, offer two ways to deliver it:

1. Merge to `main`, push `main`, then delete the worktree and its branch, locally and on the remote.
2. Push the branch and open a pull request.

## Public repo hygiene

This repo is public. Keep it free of anything private:

- No employer or work-project references (company, product or project names, ticket keys) in code, docs, commits or issues. Use `PROJ-` for example ticket keys.
- No real usage data, local paths or usernames. Use `/Users/me/...` in examples.
- Commits use the GitHub noreply email.
- No secrets.
