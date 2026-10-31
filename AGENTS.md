## Agent skills

### Issue tracker

GitHub Issues on this repo, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five roles, label = role name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `GLOSSARY.md` + `docs/adr/`. See `docs/agents/domain.md`.

## Git workflow

Single-developer project: never create pull requests. When work is done, suggest merging to `main` and pushing instead.

Create every worktree under `.claude/worktrees/`.
