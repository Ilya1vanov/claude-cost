## Agent skills

### Issue tracker

GitHub Issues on this repo, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five roles, label = role name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `GLOSSARY.md` + `docs/adr/`. See `docs/agents/domain.md`.

## Git workflow

Single-developer project: never create pull requests.

Make every change in a worktree under `.claude/worktrees/`, never in the main checkout.

When work is done, offer to finish it in one step: merge to `main`, push `main`, then delete the worktree and its branch, locally and on the remote.
