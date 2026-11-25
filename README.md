# claude-cost

Local, AI-free analysis of what Claude Code usage costs. Terms are defined in `GLOSSARY.md`; request ownership in `docs/adr/0001-request-ownership.md`.

## Install

Open Terminal, paste this line and press Enter:

```
curl -fsSL https://raw.githubusercontent.com/Ilya1vanov/claude-cost/main/install.sh | sh
```

It installs `uv` (the tool that installs claude-cost) if you do not have it, then installs the latest release and tells you the installed version. Open the dashboard with `claude-cost serve`. You do not need `git`. Running the line again is safe and leaves you on the latest release.

Works on macOS, and on Linux (best effort). Windows is not supported yet.

Technical users can skip the script and install the wheel of the latest [Release](https://github.com/Ilya1vanov/claude-cost/releases/latest) directly:

```
uv tool install --force <wheel URL from the latest Release>
```

## Update

Run `claude-cost update`. It shows what changed, asks you to confirm (`--yes` skips the question), backs up your data, installs the new version and upgrades your data. If anything fails, your data is left as it was and the previous version is reinstalled. claude-cost also tells you, at most once a day, when a newer release exists. Already on the latest release? `update` says so and does nothing. Re-running the install line also works.

## Uninstall

Run `claude-cost uninstall`. It removes claude-cost and asks whether to also delete your data (Spotlights, settings); the default keeps it. `--purge` deletes the data without asking. It never removes `uv`.

By hand: `uv tool uninstall claude-cost`. Your data is the claude-cost data folder (`store.sqlite` and its backups; `CLAUDE_COST_DATA_DIR` moves it) and the settings file `config.toml` (`claude-cost config path` prints where); delete them to remove everything.

## Usage

```
claude-cost today      # today's spend
claude-cost serve      # dashboard on http://127.0.0.1:8765/; reuses an open Chrome tab (macOS), else opens one
claude-cost serve --browser=none  # same, without opening a browser (or BROWSER=none)
claude-cost --version  # installed version
claude-cost update [--yes]        # install the latest release, backing up and upgrading your data
claude-cost uninstall [--purge]   # remove claude-cost; asks before deleting your data
claude-cost migrate    # upgrade the data store to this version's format (also done automatically on use)

# Spotlights from Bash (any Claude session can drive these; no LLM or Jira inside claude-cost)
claude-cost spotlight list [--json]
claude-cost spotlight show "Sprint 5" [--json]
claude-cost spotlight rename "Sprint 5" "Sprint 6"     # any Spotlight; names need not be unique
claude-cost spotlight search PROJ-12 [--json]        # same ranking as the web picker; prints full session ids
claude-cost spotlight create "Sprint 5" [--session ID]... [--include SPOTLIGHT]...
claude-cost spotlight add "Sprint 5" ID...           # Picked sessions
claude-cost spotlight remove "Sprint 5" ID...
claude-cost spotlight include "Sprint 5" PROJ-12 PROJ-13   # Included spotlights, one level of nesting
claude-cost spotlight exclude "Sprint 5" PROJ-13
claude-cost spotlight delete "Sprint 5"
```

Every Spotlight command takes a Spotlight by id, key or name (a name only while exactly one Spotlight has it; otherwise it fails listing the ids). Spotlight commands ingest first, call the same `QueryService` operations as the web, and on a bad name, session id or nesting print one `claude-cost: ...` line to stderr and exit 1. `--json` shapes: `list` is an array of `{id, name, key, kind, total, requests, sessions, includes, first_activity, last_activity}`; `show` is that object plus `picked` (ids), `included` (`{id, name, kind, cost, sessions}`) and `members` (`{id, label, cost, requests, joined_as, picked, key_matched, via}`); `search` is an array of `{id, label, cost, kind}`.

## Limitations

claude-cost counts only what local Claude Code transcripts record, so it can come in below your bill:

- **This machine only.** Sessions on other machines, in the cloud or in CI are not counted.
- **Claude Code only.** Cowork and Chat usage is not counted.
- **Deleted transcripts.** A request counts only if it was ingested before its transcript was removed (for example by Claude Code's `cleanupPeriodDays` or a deleted worktree). `ingest --rebuild` re-reads only the transcripts that still exist, so it drops those requests.
- **Runs that leave no transcript in a Data root.** `claude plugin eval` runs its sessions in a sandbox, so their cost is not counted, whether you start them by hand or from a Claude session.
- **Static prices.** Prices come from a table in `pricing.py` and are not fetched or updated automatically. Each model has a single price with no history, so a price change also reprices past requests. A model missing from the table costs $0 and is listed under "Unpriced models". A price override in `config.toml` replaces the whole entry, including any long-context rate.
- **Cache-write duration.** When a transcript records cache writes without splitting them into 5-minute and 1-hour writes, the unsplit part is priced at the 5-minute rate.

## Developer conventions

**Stack:** Python >= 3.11 managed with `uv`, SQLite (stdlib), Flask with server-rendered Jinja pages, Chart.js 4.4.7 vendored at `src/claude_cost/static/vendor/` (no CDN, no Node build); the only network call is a daily update check: one request to the GitHub Releases API for the latest release, which sends no usage data or identifiers; turn it off with `update_check = false` in `config.toml` or `CLAUDE_COST_UPDATE_CHECK=0` for no network calls at all, pytest.

**Run tests:** `uv run pytest -q` (includes a `uv tool install` check that uses temp `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR`).

**Layout** (`src/claude_cost/`):

| Module | Role |
|---|---|
| `config.py` | `load_config(env)`: Data roots, store path, timezone, port from `config.toml` |
| `sources.py` | finds transcripts under a Data root (on-disk shapes live here) |
| `ingest.py` | parses transcripts, writes Requests to the store |
| `ownership.py` | ADR-0001 owner rule |
| `pricing.py` | dated price table, `price_request` |
| `store.py` | SQLite schema, ordered migration steps (`MIGRATIONS`) + `connect` |
| `findings/` | Drains and Opportunities, with their thresholds |
| `spotlights.py` | Spotlight logic (pure): keys, membership, Included spotlights, picker ranking |
| `query.py` | `QueryService`, the only source of numbers for web and CLI |
| `web.py` / `templates/` / `static/` | Flask adapter (`create_app`) |
| `cli.py` | argparse adapter (`main`), including the `spotlight` group |

**Rules:** the web and CLI never query SQLite directly; they call `QueryService`. Config, clock, server and browser are injected (`env`, `now`, `run_server`, `open_browser`, `focus_tab`, `probe_server`) so tests never touch real state. Tests never use real `~/.claude` or user config: an autouse fixture in `tests/conftest.py` points `HOME`, `CLAUDE_COST_CONFIG_DIR` and `CLAUDE_COST_DATA_DIR` at temp dirs. `CLAUDE_COST_CONFIG_DIR` / `CLAUDE_COST_DATA_DIR` also work for real use.

**Add a dimension (group-by/filter):** add the column through a store migration (below) and the insert in `ingest.py`, expose a query method on `QueryService` that groups by it, test it through the service.

**Change the store schema:** append one function to `MIGRATIONS` in `store.py` that takes the store from version N-1 to N (it runs inside the migration transaction: no `commit`, no `executescript`). Never edit `SCHEMA` (the version-1 shape) or an earlier step. The store's `PRAGMA user_version` is the number of steps applied; opening a store runs every pending step in one transaction (a failure rolls all back and leaves the file unchanged), and a store with a newer version than the code knows is refused. `claude-cost migrate` opens the configured store and migrates it; every other command migrates through the same path. Test with a real SQLite file in the previous shape (see `tests/test_store_migrations.py`).

**Add a page:** add a method to `QueryService` for the data, a route in `web.py`, a template in `templates/`, and a line in `tests/test_adapters.py` that loads the page.

**Add a CLI command:** add a `sub.add_parser(...)` and a `cmd_*` function in `cli.py` that calls `QueryService`; add a smoke test calling `cli.main([...], env=..., now=..., out=...)`.

**Add a price/model:** append a `PriceEntry` (with `effective_from`) to `PRICE_TABLE` in `pricing.py`.

**Fixture helpers** (`tests/helpers.py`, fixtures in `tests/conftest.py`):

```python
def test_x(root, service_for):                    # root = synthetic Data root in tmp_path
    root.write_transcript("proj", "sess-1", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000)])
    root.write_subagent_transcript("proj", "sess-1", "a1", [...])
    svc = service_for(root)                       # ingests into a temp store, UTC QueryService
    assert svc.daily_spend(day, day)[0].cost == pytest.approx(20.0)
```

`req(...)` takes `model`, `input`, `output`, `cache_read`, `write_5m`, `write_1h`, `lines` (streamed split), `session_id` (record `sessionId`, for copies). `write_transcript(..., extra_lines=[raw lines])` injects corrupt or other record types. For CLI/web tests use `configure(root)` (writes a temp `config.toml`, returns env) and `NOW` from `conftest`.
