"""CLI: thin adapter over the query service. Add a command = add a subparser + a function."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import threading
import webbrowser
from datetime import date, datetime, timedelta
from importlib import metadata
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import ingest as ingest_mod
from . import localtime
from . import pricing
from . import store
from . import config as config_mod
from .config import Config, ConfigError, load_config
from .findings import DRAIN, all_thresholds, NOT_ADDITIVE_NOTE, OPPORTUNITY
from .query import PeriodComparison, QueryService
from .spotlights import SpotlightError

HOST = "127.0.0.1"  # never bind anywhere else: prompts and titles stay on the machine


def _version() -> str:
    try:
        return metadata.version("claude-cost")
    except metadata.PackageNotFoundError:
        return "unknown"


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
            if f.table:  # one line per row; a collapsible cell shows its summary
                lines += ["    " + "  ".join(c if isinstance(c, str) else c.summary for c in row)
                          for row in f.table.rows]
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
                     f"payback {payback}, autoCompactWindow {m.autocompact_window:,}"
                     + (f" ({m.window_note})" if m.window_note else ""))
        if m.model is None or m.source == "model":
            lines.append("  cost vs threshold: " + ", ".join(
                f"{c.threshold // 1000}K=${c.cost:.2f}" for c in m.curve) +
                f" (observed ${m.actual_cost:.2f})")
    lines.append(f"Claude Code settings (autocompact fires {analysis.AUTOCOMPACT_OFFSET:,} "
                 "tokens before the window):")
    lines.append(analysis.model_settings_json())
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
                                 desktop_stores=config.desktop_stores,
                                 compact_settings=tuple(
                                     config.thresholds.get(n, all_thresholds()[n].default)
                                     for n in ("compact_min_sample", "compact_step")),
                                 key_pattern=config.spotlight_key_pattern)
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


def _date(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _summary_json(s) -> dict:
    return {"id": s.id, "name": s.name, "key": s.key, "kind": s.kind, "locked": s.locked, "total": round(s.total, 4), "requests": s.requests,
            "sessions": s.sessions, "includes": s.includes,
            "first_activity": _date(s.first_activity), "last_activity": _date(s.last_activity)}


def _print_spotlight_list(summaries, as_json: bool, out) -> None:
    if as_json:
        print(json.dumps([_summary_json(s) for s in summaries], indent=2), file=out)
        return
    for s in summaries:
        includes = f"  includes {s.includes}" if s.includes else ""
        key = f"  key {s.key}" if s.key and s.key != s.name else ""
        lock = "  locked" if s.locked else ""
        print(f"{s.id}  {s.name}{key}  {s.kind}{lock}  {_money(s.total)}  {s.sessions} sessions{includes}", file=out)


def _print_spotlight_detail(detail, as_json: bool, out) -> None:
    if as_json:
        included = [{"id": i.id, "name": i.name, "kind": i.kind, "cost": round(i.cost, 4), "sessions": i.sessions}
                    for i in detail.included]
        members = [{"id": m.session_id, "label": m.label, "cost": round(m.cost, 4),
                    "requests": m.requests, "joined_as": m.joined_as, "picked": m.picked,
                    "key_matched": m.key_matched, "via": m.via} for m in detail.sessions]
        dismissed = [{"id": d.session_id, "label": d.label, "cost": round(d.cost, 4)} for d in detail.dismissed]
        suggested = [{"id": x.session_id, "label": x.label, "cost": round(x.cost, 4),
                      "field": x.field, "snippet": x.snippet} for x in detail.suggested]
        print(json.dumps({**_summary_json(detail.summary), "picked": list(detail.picked),
                          "included": included, "members": members, "suggested": suggested,
                          "dismissed": dismissed}, indent=2), file=out)
        return
    summary = detail.summary
    key = f"  key {summary.key}" if summary.key and summary.key != summary.name else ""
    lock = "  locked" if summary.locked else ""
    print(f"{summary.id}  {summary.name}{key}  {summary.kind}{lock}  {_money(summary.total)}  "
          f"{summary.sessions} sessions, {summary.requests} requests", file=out)
    for inc in detail.included:
        print(f"  includes {inc.name}  {_money(inc.cost)}  {inc.sessions} sessions", file=out)
    for m in detail.sessions:
        print(f"  {m.session_id}  {_money(m.cost)}  {m.label}  [{m.joined_as}]", file=out)
    for x in detail.suggested:
        print(f"  suggested {x.session_id}  {_money(x.cost)}  {x.label}  {x.field}: {x.snippet}", file=out)
    for d in detail.dismissed:
        print(f"  dismissed {d.session_id}  {_money(d.cost)}  {d.label}", file=out)


def _print_suggestions(groups, as_json: bool, out) -> None:
    if as_json:
        print(json.dumps([{"key": g.key, "spotlight": g.spotlight_id, "name": g.name,
                           "suggested": [{"id": x.session_id, "label": x.label, "cost": round(x.cost, 4),
                                          "field": x.field, "snippet": x.snippet} for x in g.suggested]}
                          for g in groups], indent=2), file=out)
        return
    for g in groups:
        target = g.spotlight_id or "no Auto-spotlight yet"
        print(f"{g.key}  ({target})", file=out)
        for x in g.suggested:
            print(f"  {x.session_id}  {_money(x.cost)}  {x.label}  {x.field}: {x.snippet}", file=out)


def _print_session_search(found, as_json: bool, out) -> None:
    if as_json:
        print(json.dumps([{"id": c.session_id, "label": c.label, "cost": round(c.cost, 4), "kind": c.kind}
                          for c in found], indent=2), file=out)
        return
    for c in found:
        kind = f"  [{c.kind}]" if c.kind else ""
        print(f"{c.session_id}  {_money(c.cost)}  {c.label}{kind}", file=out)


def cmd_spotlight(args, config: Config, now: datetime, out, err) -> int:
    """`spotlight` group: each subcommand calls the QueryService operation the web uses, which
    validates names and Sessions and raises SpotlightError."""
    svc = _service(config, now, err)  # ingest first, so new Sessions can be picked
    action = args.spotlight_action
    if action == "list" and args.dismissed:
        rows = svc.dismissed_key_rows()
        if args.json:
            print(json.dumps([{"key": k, "id": i, "name": n} for k, i, n in rows], indent=2), file=out)
        else:
            for k, i, n in rows:
                print(f"{i}  {n}  key {k}" if n != k else f"{i}  {n}", file=out)
    elif action == "list":
        _print_spotlight_list(svc.spotlights(), args.json, out)
    elif action == "dismiss-key":
        svc.dismiss_key(args.key)
    elif action == "restore-key":
        svc.restore_key(args.key)
    elif action == "show":
        try:
            detail = svc.spotlight(args.name)
        except KeyError:
            raise SpotlightError(f"no Spotlight named {args.name!r}") from None
        _print_spotlight_detail(detail, args.json, out)
    elif action == "rename":
        svc.rename_spotlight(args.name, args.new_name)
    elif action == "lock":
        svc.lock_spotlight(args.name)
    elif action == "unlock":
        svc.unlock_spotlight(args.name)
    elif action == "suggestions":
        _print_suggestions(svc.suggestions(), args.json, out)
    elif action == "search":
        _print_session_search(svc.search_sessions(args.text, ""), args.json, out)
    elif action == "create":
        svc.create_spotlight(args.name, args.session, args.include)
    elif action == "delete":
        svc.delete_spotlight(args.name)
    elif action == "add":
        svc.add_sessions(args.name, args.session, create=False)
    elif action == "remove":
        for session_id in args.session:
            svc.remove_session(args.name, session_id)
    elif action == "restore":
        for session_id in args.session:
            svc.restore_session(args.name, session_id)
    elif action == "include":
        for other in args.other:
            svc.include_spotlight(args.name, other)
    elif action == "exclude":
        for other in args.other:
            svc.exclude_spotlight(args.name, other)
    return 0


# Select the first Chrome tab on the dashboard's origin, raise its window and reload it.
# Never launches Chrome: a closed Chrome means there is no tab to reuse.
_FOCUS_TAB_SCRIPT = """
on run argv
    set prefix to item 1 of argv
    if application "Google Chrome" is not running then return "none"
    tell application "Google Chrome"
        repeat with w in windows
            set i to 0
            repeat with t in tabs of w
                set i to i + 1
                if URL of t starts with prefix then
                    set active tab index of w to i
                    set index of w to 1
                    tell t to reload
                    activate
                    return "focused"
                end if
            end repeat
        end repeat
    end tell
    return "none"
end run
"""


def _focus_chrome_tab(url: str) -> bool:
    """True when an open Chrome tab on `url`'s origin was focused and reloaded (macOS only).
    Any failure, such as macOS denying control of Chrome, is False so the caller opens a tab."""
    if sys.platform != "darwin":
        return False
    try:
        done = subprocess.run(["osascript", "-e", _FOCUS_TAB_SCRIPT, url],
                              capture_output=True, text=True, timeout=60)  # room for the first-run macOS prompt
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and done.stdout.strip() == "focused"


def _probe_server(url: str) -> str | None:
    """What GET /healthz answers on `url`, "" when something else holds the port, None when nothing does."""
    import http.client
    import urllib.error
    import urllib.request
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a system proxy
    try:
        with direct.open(f"{url}healthz", timeout=1) as response:
            return response.read(64).decode(errors="replace")
    except urllib.error.HTTPError:
        return ""
    except urllib.error.URLError as e:
        return None if isinstance(e.reason, ConnectionRefusedError) else ""
    except (OSError, http.client.HTTPException):
        return ""


def _run_server(app, host: str, port: int, on_ready: Callable[[], object]) -> None:
    """Bind first, then `on_ready` (the tab is shown once it can load), then serve."""
    from werkzeug.serving import make_server
    server = make_server(host, port, app, threaded=True)
    on_ready()
    server.serve_forever()


def cmd_serve(config: Config, now_fn: Callable[[], datetime], out, err, run_server: Callable,
              show_tab: Callable[[str], object], probe_server: Callable[[str], str | None]) -> int:
    from .web import HEALTH, create_app

    url = f"http://{HOST}:{config.port}/"
    answer = probe_server(url)
    if answer == HEALTH:  # a serve is already running: just bring its tab back
        print(f"Already serving {url}", file=out)
        show_tab(url)
        return 0
    if answer is not None:
        print(f"claude-cost: port {config.port} is in use by another program "
              f"(change it with `claude-cost config set port N`)", file=err)
        return 1
    lock = threading.Lock()

    def refresh() -> None:
        with lock:
            _ingest(config, now_fn())

    _ingest(config, now_fn(), err)  # ingest on start
    app = create_app(lambda: _open(config), now_fn, refresh, top_n=config.top_n)
    print(f"Serving {url}", file=out)
    run_server(app, HOST, config.port, lambda: show_tab(url))
    return 0


def cmd_config(args, path, env: Mapping[str, str], out, run_editor: Callable[[list[str]], object]) -> int:
    """`config` shows every setting; get/set/path/edit work on the file. Works even if the file is broken."""
    config_mod.ensure_config_file(path, env)
    action = args.config_action
    if action == "path":
        print(path, file=out)
    elif action == "get":
        setting = config_mod.get_setting(path, args.key, env)
        detected = "  (source: default (detected))" if setting.source == "default (detected)" else ""
        print(f"{setting.value}{detected}", file=out)
    elif action == "set":
        print(f"{args.key} = {config_mod.set_value(path, args.key, args.value)}", file=out)
    elif action == "edit":
        run_editor([*shlex.split(env.get("EDITOR") or "vi"), str(path)])
    else:
        for v in config_mod.describe_settings(path, env):
            print(f"{v.name} = {v.value}  (default: {v.default}, source: {v.source})", file=out)
    return 0


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None,
         now: datetime | None = None, out=None, err=None, run_server: Callable | None = None,
         open_browser: Callable[[str], object] = webbrowser.open,
         focus_tab: Callable[[str], bool] | None = None,
         probe_server: Callable[[str], str | None] | None = None,
         run_editor: Callable[[list[str]], object] = subprocess.run) -> int:
    """Entry point with injectable env/clock/server so tests never touch real state."""
    env = os.environ if env is None else env
    out = out or sys.stdout
    err = err or sys.stderr
    parser = argparse.ArgumentParser(prog="claude-cost")
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
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
    p_serve = sub.add_parser("serve", help="start the web dashboard on 127.0.0.1")
    p_serve.add_argument("--browser", choices=["default", "none"],
                         help="none: don't open a browser (also BROWSER=none)")
    spot = sub.add_parser("spotlight", help="list, show, create and edit Spotlights")
    spot_sub = spot.add_subparsers(dest="spotlight_action", required=True)
    sp = spot_sub.add_parser("list", help="every Spotlight with its total")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--dismissed", action="store_true", help="list Dismissed keys instead")
    for action, helptext in (("dismiss-key", "hide a key's Auto-spotlight"),
                             ("restore-key", "show a Dismissed key's Auto-spotlight again, with everything it had")):
        spot_sub.add_parser(action, help=helptext).add_argument("key")
    sp = spot_sub.add_parser("show", help="one Spotlight: included Spotlights and sessions")
    sp.add_argument("name")
    sp.add_argument("--json", action="store_true")
    sp = spot_sub.add_parser("rename", help="rename a Spotlight (any Spotlight, to a non-empty name)")
    sp.add_argument("name", help="id, key or unique name")
    sp.add_argument("new_name")
    for action, helptext in (("lock", "lock an Auto-spotlight: its Key matches become Picked sessions and "
                                      "sessions carrying its key are only suggested"),
                             ("unlock", "unlock an Auto-spotlight: Key matches join automatically again")):
        spot_sub.add_parser(action, help=helptext).add_argument("name", help="id or key of an Auto-spotlight")
    sp = spot_sub.add_parser("suggestions", help="every pending Suggested session, grouped by key")
    sp.add_argument("--json", action="store_true")
    sp = spot_sub.add_parser("search", help="find sessions by key or text, ranked as the web picker does")
    sp.add_argument("text")
    sp.add_argument("--json", action="store_true")
    sp = spot_sub.add_parser("create", help="create a Spotlight")
    sp.add_argument("name")
    sp.add_argument("--session", action="append", default=[], help="Picked session id (repeatable)")
    sp.add_argument("--include", action="append", default=[], help="Spotlight to include (repeatable)")
    spot_sub.add_parser("delete", help="delete a Spotlight").add_argument("name")
    for action, helptext in (("add", "pick sessions, accepting suggestions (clears a dismissal)"),
                             ("remove", "unpick sessions; a session carrying an Auto-spotlight's key is dismissed (declines a suggestion)"),
                             ("restore", "restore Dismissed sessions of an Auto-spotlight")):
        sp = spot_sub.add_parser(action, help=helptext)
        sp.add_argument("name")
        sp.add_argument("session", nargs="+")
    for action, helptext in (("include", "include Spotlights"), ("exclude", "stop including Spotlights")):
        sp = spot_sub.add_parser(action, help=helptext)
        sp.add_argument("name")
        sp.add_argument("other", nargs="+")
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
        config_mod.ensure_config_file(config_mod.config_path_for(env), env)
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
    if args.command == "spotlight":
        try:
            return cmd_spotlight(args, config, now_fn(), out, err)
        except SpotlightError as e:
            print(f"claude-cost: {e}", file=err)
            return 1
    if args.command == "ingest":
        try:
            return cmd_ingest(config, now_fn(), out, err, args.rebuild)
        except SpotlightError as e:
            print(f"claude-cost: {e}", file=err)
            return 1
    focus = focus_tab or _focus_chrome_tab
    if (args.browser or env.get("BROWSER")) == "none":
        show_tab = lambda url: None
    else:
        show_tab = lambda url: focus(url) or open_browser(url)
    return cmd_serve(config, now_fn, out, err, run_server or _run_server, show_tab,
                     probe_server or _probe_server)


def main_entry() -> None:
    sys.exit(main())
