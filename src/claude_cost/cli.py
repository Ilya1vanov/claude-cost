"""CLI: thin adapter over the query service. Add a command = add a subparser + a function."""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import threading
import webbrowser
from datetime import date, datetime, timedelta
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import ingest as ingest_mod
from . import localtime
from . import pricing
from . import store
from . import config as config_mod
from .config import Config, ConfigError, load_config
from .findings import DRAIN, NOT_ADDITIVE_NOTE, OPPORTUNITY
from .query import PeriodComparison, QueryService

HOST = "127.0.0.1"  # never bind anywhere else: prompts and titles stay on the machine


def _now(config: Config) -> datetime:
    return datetime.now(ZoneInfo(config.timezone) if config.timezone else None).astimezone()


def _open(config: Config) -> QueryService:
    """A service on the store; callers ingest first (which creates the schema)."""
    return QueryService(store.connect(config.store_path, schema=False), config.timezone,
                        price_table=pricing.build_table(config.prices),
                        thresholds=config.thresholds,
                        compare_model=config.fable_compare_model,
                        key_pattern=config.spotlight_key_pattern)


def parse_since(text: str, today: date) -> date:
    """`--since` value: an ISO date (2026-10-01) or a number of days back (7d)."""
    try:
        return localtime.parse_since(text, today)
    except ValueError:
        raise ConfigError(f"--since must be a date like 2026-10-01 or days like 7d, got {text!r}") from None


def format_findings(findings) -> str:
    """Drains then Opportunities; avoidable is per finding, never totalled."""
    lines = []
    for kind, heading in ((DRAIN, "Drains"), (OPPORTUNITY, "Opportunities")):
        lines.append(f"{heading}:")
        group = [f for f in findings if f.kind == kind]
        for f in group:
            avoid = "n/a" if f.avoidable is None else f"${f.avoidable:.2f}"
            lines.append(f"  {f.title}: attributed ${f.attributed:.2f}, avoidable {avoid} "
                         f"({len(f.requests)} requests, {len(f.sessions)} sessions)")
            if f.formula:
                lines.append(f"    {f.formula}" + (f" [{f.detail}]" if f.detail else ""))
        if not group:
            lines.append("  none")
    lines.append(NOT_ADDITIVE_NOTE)
    return "\n".join(lines)


def cmd_drains(config: Config, now: datetime, out, err, since: str | None) -> int:
    svc = _service(config, now, err)
    print(format_findings(svc.findings(parse_since(since, svc.today(now)) if since else None)), file=out)
    return 0


def format_compact(analysis) -> str:
    """Compact threshold per model: T, source, sample size, payback and the cost-vs-T curve."""
    if analysis.global_ is None:
        return "No compactions observed yet, so no Compact threshold can be learned."
    lines = []
    for m in (*analysis.models, analysis.global_):
        label = m.model or "all models (global)"
        payback = "n/a" if m.payback_requests is None else f"{m.payback_requests:,.0f} requests"
        lines.append(f"{label}: Compact threshold {m.threshold:,} tokens ({m.source}), "
                     f"{m.sample_size} compactions, p75 compaction ${m.compaction_cost:.2f}, "
                     f"payback {payback}")
        if m.model is None or m.source == "model":
            lines.append("  cost vs threshold: " + ", ".join(
                f"{c.threshold // 1000}K=${c.cost:.2f}" for c in m.curve) +
                f" (observed ${m.actual_cost:.2f})")
    return "\n".join(lines)


def cmd_compact(config: Config, now: datetime, out, err) -> int:
    print(format_compact(_service(config, now, err).compact_thresholds()), file=out)
    return 0


def _ingest(config: Config, now: datetime, err=None, rebuild: bool = False) -> int:
    """Run ingest; on the first run print retention warnings to `err` (stderr)."""
    conn = store.connect(config.store_path)
    try:
        if err is not None and ingest_mod.is_first_run(conn):
            for warning in ingest_mod.retention_warnings(config.data_roots):
                print(warning, file=err)
        return ingest_mod.ingest(conn, config.data_roots, rebuild=rebuild, now=now,
                                 price_table=pricing.build_table(config.prices),
                                 desktop_stores=config.desktop_stores)
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
    svc = _service(config, now, err)
    day = svc.today(now)
    spend = svc.daily_spend(day, day)[0]
    print(f"Today ({day.isoformat()}): ${spend.cost:.2f} across {spend.requests} requests", file=out)
    return 0


def _money(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):.2f}"


def format_week(c: PeriodComparison) -> str:
    partial = f" (partial: {c.elapsed_days} of {c.period_days} days)" if c.partial else ""
    pct = "n/a" if c.delta_pct is None else f"{c.delta_pct:+.1f}%"
    lines = [f"Week {c.current.start}..{c.current.end}{partial} vs {c.baseline.start}..{c.baseline.end}",
             f"Cost: ${c.current_cost:.2f} vs ${c.baseline_cost:.2f} ({_money(c.delta_cost)}, {pct})",
             "Cost components:"]
    lines += [f"  {x.name}: ${x.current:.2f} vs ${x.baseline:.2f}" for x in c.components]
    for title, rows in (("Top sessions:", c.top_sessions), ("Top projects:", c.top_projects)):
        lines.append(title)
        lines += [f"  {r.key}: ${r.cost:.2f} ({r.requests} requests)" for r in rows] or ["  none"]
    return "\n".join(lines)


def cmd_week(config: Config, now: datetime, out, err) -> int:
    service = _service(config, now, err)
    print(format_week(service.period_comparison("week", now, top_n=config.top_n)), file=out)
    return 0


def cmd_prices(config: Config, now: datetime, out, err) -> int:
    svc = _service(config, now, err)
    print("Prices in effect ($ per million tokens)", file=out)
    print(f"{'model':<22}{'from':<12}{'input':>8}{'output':>8}{'write5m':>9}{'write1h':>9}"
          f"{'read':>8}  note", file=out)
    for r in svc.price_table():
        note = " ".join(x for x in (r.tier_note, "(config)" if r.source == "config" else "") if x)
        print(f"{r.model:<22}{r.effective_from.isoformat():<12}{r.input:>8.4g}{r.output:>8.4g}"
              f"{r.cache_write_5m:>9.4g}{r.cache_write_1h:>9.4g}{r.cache_read:>8.4g}  {note}".rstrip(),
              file=out)
    unpriced = svc.unpriced_models()
    print("Unpriced models found: " + (", ".join(unpriced) if unpriced else "none"), file=out)
    return 0


def cmd_serve(config: Config, now_fn: Callable[[], datetime], out, err,
              run_server: Callable | None, open_browser: Callable[[str], object]) -> int:
    from .web import create_app

    lock = threading.Lock()

    def refresh() -> None:
        with lock:
            _ingest(config, now_fn())

    _ingest(config, now_fn(), err)  # ingest on start
    app = create_app(lambda: _open(config), now_fn, refresh, top_n=config.top_n)
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
    sub.add_parser("week", help="print this calendar week's spend compared with the previous week")
    p_drains = sub.add_parser("drains", help="list findings: Drains and Opportunities")
    p_drains.add_argument("--since", help="only requests from this date (2026-10-01) or N days back (7d)")
    sub.add_parser("compact", help="show the learned Compact threshold per model")
    sub.add_parser("prices", help="list the price table in effect and unpriced models")
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
    if args.command == "week":
        return cmd_week(config, now_fn(), out, err)
    if args.command == "compact":
        return cmd_compact(config, now_fn(), out, err)
    if args.command == "prices":
        return cmd_prices(config, now_fn(), out, err)
    if args.command == "drains":
        try:
            return cmd_drains(config, now_fn(), out, err, args.since)
        except ConfigError as e:
            print(f"claude-cost: {e}", file=err)
            return 2
    if args.command == "ingest":
        return cmd_ingest(config, now_fn(), out, err, args.rebuild)
    return cmd_serve(config, now_fn, out, err, run_server, open_browser)


def main_entry() -> None:
    sys.exit(main())
