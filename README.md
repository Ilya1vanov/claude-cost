# claude-cost

Local, AI-free analysis of what Claude Code usage costs. Terms are defined in `GLOSSARY.md`; request ownership in `docs/adr/0001-request-ownership.md`.

```
uv tool install git+https://github.com/Ilya1vanov/claude-cost
claude-cost today      # today's spend
claude-cost serve      # dashboard on http://127.0.0.1:8765/
```

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
| `query.py` | `QueryService`, the only source of numbers for web and CLI |
| `web.py` / `templates/` / `static/` | Flask adapter (`create_app`) |
| `cli.py` | argparse adapter (`main`) |

Not yet created, add under these names: `findings.py`, `spotlights.py`.

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
