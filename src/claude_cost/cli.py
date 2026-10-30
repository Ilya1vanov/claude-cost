"""CLI: thin adapter over the query service. Add a command = add a subparser + a function."""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import ingest as ingest_mod
from . import store
from . import config as config_mod
from .config import Config, ConfigError, load_config
from .query import QueryService

HOST = "127.0.0.1"  # never bind anywhere else: prompts and titles stay on the machine


def _now(config: Config) -> datetime:
    return datetime.now(ZoneInfo(config.timezone) if config.timezone else None).astimezone()


def _open(config: Config) -> QueryService:
    return QueryService(store.connect(config.store_path), config.timezone)


def _ingest(config: Config, now: datetime, err=None, rebuild: bool = False) -> int:
    """Run ingest; on the first run print retention warnings to `err` (stderr)."""
    conn = store.connect(config.store_path)
    try:
        if err is not None and ingest_mod.is_first_run(conn):
            for warning in ingest_mod.retention_warnings(config.data_roots):
                print(warning, file=err)
        return ingest_mod.ingest(conn, config.data_roots, rebuild=rebuild, now=now)
    finally:
        conn.close()


def _service(config: Config, now: datetime, err=None) -> QueryService:
    _ingest(config, now, err)
    return _open(config)


def cmd_ingest(config: Config, now: datetime, out, err, rebuild: bool) -> int:
    total = _ingest(config, now, err, rebuild)
    print(f"Ingested: {total} requests in the store", file=out)
    return 0


def cmd_today(config: Config, now: datetime, out, err) -> int:
    day = now.date()
    spend = _service(config, now, err).daily_spend(day, day)[0]
    print(f"Today ({day.isoformat()}): ${spend.cost:.2f} across {spend.requests} requests", file=out)
    return 0


def cmd_serve(config: Config, now_fn: Callable[[], datetime], out, err,
              run_server: Callable | None, open_browser: Callable[[str], object]) -> int:
    from .web import create_app

    lock = threading.Lock()

    def refresh() -> None:
        with lock:
            _ingest(config, now_fn())

    _ingest(config, now_fn(), err)  # ingest on start
    app = create_app(lambda: _open(config), now_fn, refresh)
    url = f"http://{HOST}:{config.port}/"
    print(f"Serving {url}", file=out)
    open_browser(url)
    if run_server is None:
        app.run(host=HOST, port=config.port)
    else:
        run_server(app, HOST, config.port)
    return 0


def cmd_config(args, path, env: Mapping[str, str], out, run_editor: Callable[[list[str]], object]) -> int:
    """`config` shows every setting; get/set/path/edit work on the file. Works even if the file is broken."""
    config_mod.ensure_config_file(path)
    action = args.config_action
    if action == "path":
        print(path, file=out)
    elif action == "get":
        print(config_mod.get_value(path, args.key), file=out)
    elif action == "set":
        print(f"{args.key} = {config_mod.set_value(path, args.key, args.value)}", file=out)
    elif action == "edit":
        run_editor([*shlex.split(env.get("EDITOR") or "vi"), str(path)])
    else:
        for v in config_mod.describe_settings(path):
            print(f"{v.name} = {v.value}  (default: {v.default}, source: {v.source})", file=out)
    return 0


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None,
         now: datetime | None = None, out=None, err=None, run_server: Callable | None = None,
         open_browser: Callable[[str], object] = webbrowser.open,
         run_editor: Callable[[list[str]], object] = subprocess.run) -> int:
    """Entry point with injectable env/clock/server so tests never touch real state."""
    env = os.environ if env is None else env
    out = out or sys.stdout
    err = err or sys.stderr
    parser = argparse.ArgumentParser(prog="claude-cost")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("today", help="print today's spend")
    p_ingest = sub.add_parser("ingest", help="read new transcript data into the store")
    p_ingest.add_argument("--rebuild", action="store_true",
                          help="re-read every transcript that still exists and recompute all requests")
    sub.add_parser("serve", help="start the web dashboard on 127.0.0.1")
    cfg = sub.add_parser("config", help="show or change settings in config.toml")
    cfg_sub = cfg.add_subparsers(dest="config_action")
    cfg_sub.add_parser("path", help="print the config file location")
    cfg_sub.add_parser("edit", help="open the config file in $EDITOR")
    cfg_get = cfg_sub.add_parser("get", help="print one setting")
    cfg_get.add_argument("key")
    cfg_set = cfg_sub.add_parser("set", help="validate and write one setting")
    cfg_set.add_argument("key")
    cfg_set.add_argument("value")
    args = parser.parse_args(argv)

    try:
        if args.command == "config":
            return cmd_config(args, config_mod.config_path_for(env), env, out, run_editor)
        config_mod.ensure_config_file(config_mod.config_path_for(env))
        config = load_config(env)
    except ConfigError as e:
        print(f"claude-cost: {e}", file=err)
        return 2
    now_fn = (lambda: now) if now else (lambda: _now(config))
    if args.command == "today":
        return cmd_today(config, now_fn(), out, err)
    if args.command == "ingest":
        return cmd_ingest(config, now_fn(), out, err, args.rebuild)
    return cmd_serve(config, now_fn, out, err, run_server, open_browser)


def main_entry() -> None:
    sys.exit(main())
