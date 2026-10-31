# claude-cost

Local, AI-free analysis of what Claude Code usage costs. Terms are defined in `GLOSSARY.md`; request ownership in `docs/adr/0001-request-ownership.md`.

```
uv tool install git+https://github.com/Ilya1vanov/claude-cost
claude-cost today      # today's spend
claude-cost serve      # dashboard on http://127.0.0.1:8765/
claude-cost serve --browser=none  # same, without opening a browser (or BROWSER=none)
claude-cost --version  # installed version

# Spotlights from Bash (any Claude session can drive these; no LLM or Jira inside claude-cost)
claude-cost spotlight list [--json]
claude-cost spotlight show "Sprint 5" [--json]
claude-cost spotlight search PROJ-12 [--json]        # same ranking as the web picker; prints full session ids
claude-cost spotlight create "Sprint 5" [--session ID]... [--include SPOTLIGHT]...
claude-cost spotlight add "Sprint 5" ID...           # Picked sessions
claude-cost spotlight remove "Sprint 5" ID...
claude-cost spotlight include "Sprint 5" PROJ-12 PROJ-13   # Included spotlights, one level of nesting
claude-cost spotlight exclude "Sprint 5" PROJ-13
claude-cost spotlight delete "Sprint 5"
```

Spotlight commands ingest first, call the same `QueryService` operations as the web, and on a bad name, session id or nesting print one `claude-cost: ...` line to stderr and exit 1. `--json` shapes: `list` is an array of `{name, kind, total, requests, sessions, includes, first_activity, last_activity}`; `show` is that object plus `picked` (ids), `included` (`{name, kind, cost, sessions}`) and `members` (`{id, label, cost, requests, joined_as, picked, key_matched, via}`); `search` is an array of `{id, label, cost, kind}`.

## Developer conventions

**Stack:** Python >= 3.11 managed with `uv`, SQLite (stdlib), Flask with server-rendered Jinja pages, Chart.js 4.4.7 vendored at `src/claude_cost/static/vendor/` (no CDN, no network calls, no Node build), pytest.

**Run tests:** `uv run pytest -q` (includes a `uv tool install` check that uses temp `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR`).

**Layout** (`src/claude_cost/`):

| Module | Role |
|---|---|
| `config.py` | `load_config(env)`: Data roots, store path, timezone, port from `config.toml` |
| `sources.py` | finds transcripts under a Data root (on-disk shapes live here) |
| `ingest.py` | parses transcripts, writes Requests to the store |
| `ownership.py` | ADR-0001 owner rule |
| `pricing.py` | dated price table, `price_request` |
| `store.py` | SQLite schema + `connect` |
| `findings/` | Drains and Opportunities, with their thresholds |
| `spotlights.py` | Spotlight logic (pure): keys, membership, Included spotlights, picker ranking |
| `query.py` | `QueryService`, the only source of numbers for web and CLI |
| `web.py` / `templates/` / `static/` | Flask adapter (`create_app`) |
| `cli.py` | argparse adapter (`main`), including the `spotlight` group |

**Rules:** the web and CLI never query SQLite directly; they call `QueryService`. Config, clock, server and browser are injected (`env`, `now`, `run_server`, `open_browser`) so tests never touch real state. Tests never use real `~/.claude` or user config: an autouse fixture in `tests/conftest.py` points `HOME`, `CLAUDE_COST_CONFIG_DIR` and `CLAUDE_COST_DATA_DIR` at temp dirs. `CLAUDE_COST_CONFIG_DIR` / `CLAUDE_COST_DATA_DIR` also work for real use.

**Add a dimension (group-by/filter):** add the column in `store.py` `SCHEMA` (and the insert in `ingest.py`), expose a query method on `QueryService` that groups by it, test it through the service.

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
