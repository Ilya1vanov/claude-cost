---
name: verify
description: Run the claude-cost dashboard from a worktree against real Claude data, isolated from the user's own store, to observe a change at its surface.
---

# Verify claude-cost

Serve the branch on its own port and store; the Data roots stay the real `~/.claude` (read only).

```bash
S=<scratch dir>; mkdir -p $S/cfg $S/data && printf 'port = 8799\n' > $S/cfg/config.toml
CLAUDE_COST_CONFIG_DIR=$S/cfg CLAUDE_COST_DATA_DIR=$S/data uv run claude-cost serve --browser=none   # run in background
```

- The first page load on a fresh store ingests everything first: expect ~2-3 minutes before the first response (`curl -m 500`). Later loads take 1-2 s.
- To compare with `main`, serve the main checkout on another port with a copy of the same `store.sqlite` (`cp` it into a second data dir) so neither run ingests from scratch.
- Periods: `/?period=last7|week|month`, or `/?period=custom&start=YYYY-MM-DD&end=YYYY-MM-DD`. A custom Period before the earliest request gives the empty case; one starting on the earliest day gives an empty baseline.
- Cards are `<section class="card" id="...">`; read them with `document.getElementById(id).innerText`. Tooltips are `title` attributes on `.info`. The sticky nav covers a card scrolled to the top, so scroll up a little before taking a screenshot.
- Stop the servers by killing whatever listens on their ports.
