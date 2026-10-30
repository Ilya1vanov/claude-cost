"""CLI: thin adapter over the query service. Add a command = add a subparser + a function."""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from datetime import datetime
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import ingest as ingest_mod
from . import store
from .config import Config, load_config
from .query import QueryService

HOST = "127.0.0.1"  # never bind anywhere else: prompts and titles stay on the machine


def _now(config: Config) -> datetime:
    return datetime.now(ZoneInfo(config.timezone) if config.timezone else None).astimezone()


def _service(config: Config) -> QueryService:
    conn = store.connect(config.store_path)
    ingest_mod.ingest(conn, config.data_roots)
    return QueryService(conn, config.timezone)


def cmd_today(config: Config, now: datetime, out) -> int:
    day = now.date()
    spend = _service(config).daily_spend(day, day)[0]
    print(f"Today ({day.isoformat()}): ${spend.cost:.2f} across {spend.requests} requests", file=out)
    return 0


def cmd_serve(config: Config, now_fn: Callable[[], datetime], out,
              run_server: Callable | None, open_browser: Callable[[str], object]) -> int:
    from .web import create_app

    def service_factory() -> QueryService:
        return _service(config)

    app = create_app(service_factory, now_fn)
    url = f"http://{HOST}:{config.port}/"
    print(f"Serving {url}", file=out)
    open_browser(url)
    if run_server is None:
        app.run(host=HOST, port=config.port)
    else:
        run_server(app, HOST, config.port)
    return 0


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None,
         now: datetime | None = None, out=None, run_server: Callable | None = None,
         open_browser: Callable[[str], object] = webbrowser.open) -> int:
    """Entry point with injectable env/clock/server so tests never touch real state."""
    env = os.environ if env is None else env
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="claude-cost")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("today", help="print today's spend")
    sub.add_parser("serve", help="start the web dashboard on 127.0.0.1")
    args = parser.parse_args(argv)

    config = load_config(env)
    now_fn = (lambda: now) if now else (lambda: _now(config))
    if args.command == "today":
        return cmd_today(config, now_fn(), out)
    return cmd_serve(config, now_fn, out, run_server, open_browser)


def main_entry() -> None:
    sys.exit(main())
