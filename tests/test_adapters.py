"""Seam 2: adapter smoke tests. No logic asserted beyond shape."""
import html as htmllib
import io
import re

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req


def _root_with_spend(root):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000)])
    return root


def test_cli_today_prints_todays_spend(root, configure):
    env = configure(_root_with_spend(root))
    out = io.StringIO()
    assert cli.main(["today"], env=env, now=NOW, out=out) == 0
    assert out.getvalue().strip() == "Today (2026-10-30): $20.00 across 1 requests"


def test_dashboard_page_has_chart_and_served_vendored_script(root, configure, tmp_path):
    env = configure(_root_with_spend(root))
    cfg = cli.load_config(env)
    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    client = create_app(factory, lambda: NOW).test_client()
    page = client.get("/")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert 'id="daily-spend"' in html and "2026-10-30" in html
    for src in ("/static/vendor/chart.umd.js", "/static/dashboard.js"):
        assert src in html and client.get(src).status_code == 200
    assert "http://" not in html and "https://" not in html  # no CDN, no network


def test_serve_binds_loopback_only_and_opens_browser(root, configure):
    env = configure(_root_with_spend(root))
    seen, opened = {}, []
    cli.main(["serve"], env=env, now=NOW, out=io.StringIO(),
             run_server=lambda app, host, port, on_ready: (seen.update(host=host, port=port), on_ready()),
             open_browser=opened.append)
    assert seen["host"] == "127.0.0.1"
    assert opened == [f"http://127.0.0.1:{seen['port']}/"]


def test_cli_prices_lists_table_overrides_and_unpriced_models(root, configure):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", model="claude-mystery-9", output=5)])
    env = configure(root)
    from pathlib import Path
    with (Path(env["CLAUDE_COST_CONFIG_DIR"]) / "config.toml").open("a") as f:
        f.write('\n[prices.claude-opus-5-5]\ninput = 1.0\noutput = 3.0\n')
    out = io.StringIO()
    assert cli.main(["prices"], env=env, now=NOW, out=out) == 0
    text = out.getvalue()
    assert "claude-haiku-5-5" in text and "above 100000 prompt tokens" in text
    assert any(l.startswith("claude-opus-5-5") and "(config)" in l for l in text.splitlines())
    assert text.strip().endswith("Unpriced models found: claude-mystery-9")


def _client(env, top_n=10):
    cfg = cli.load_config(env)
    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    return create_app(factory, lambda: NOW, top_n=top_n).test_client()


def test_cli_week_prints_weekly_view_with_deltas(root, configure):
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T01:00:00Z", output=3_000_000)])
    root.write_transcript("p", "s0", [req("m0", "2026-10-22T01:00:00Z", output=2_000_000)])
    out = io.StringIO()
    assert cli.main(["week"], env=configure(root), now=NOW, out=out) == 0
    text = out.getvalue()
    assert "2026-10-26..2026-10-30" in text and "partial" in text
    assert "$60.00" in text and "$40.00" in text and "+$20.00" in text and "+50.0%" in text
    assert "output" in text and "s1" in text and "p" in text


def test_dashboard_shows_period_modes_comparison_and_top_n(root, configure):
    root.write_transcript("proj-x", "sess-x", [req("m1", "2026-10-29T01:00:00Z", output=1_000_000)])
    client = _client(configure(root))
    for q in ("", "?period=week", "?period=month", "?period=last7&include_today=0"):
        assert client.get("/" + q).status_code == 200
    html = client.get("/?period=week").get_data(as_text=True)
    assert "partial" in html and "sess-x" in html and "proj-x" in html and "cache read" in html
    default = client.get("/").get_data(as_text=True)
    assert 'name="include_today" value="1" checked' in default
    assert 'name="include_today" value="1" checked' not in client.get(
        "/?include_today=0").get_data(as_text=True)
    assert client.get("/?period=bogus").status_code == 400


def test_dashboard_top_n_comes_from_config(root, configure):
    for i in range(3):
        root.write_transcript("p", f"sess-{i}", [req(f"m{i}", "2026-10-29T01:00:00Z", output=1_000_000 + i)])
    html = _client(configure(root), top_n=2).get("/").get_data(as_text=True)
    assert html.count(">sess-") == 2  # link text; the href repeats the id


def test_compare_page_selects_periods_and_baseline(root, configure):
    root.write_transcript("p", "s1", [req("m1", "2026-10-13T01:00:00Z", output=1_000_000)])
    root.write_transcript("p", "s2", [req("m2", "2026-10-26T01:00:00Z", output=2_000_000)])
    client = _client(configure(root))
    assert client.get("/compare").status_code == 200
    html = client.get("/compare?mode=week&p=2026-10-12&p=2026-10-26&baseline=2026-10-26"
                      ).get_data(as_text=True)
    assert 'id="compare-chart"' in html and "$20.00" in html and "$40.00" in html
    assert "-50.0%" in html and "-$20.00" in html
    assert client.get("/compare?mode=week&p=2026-10-12&p=2026-10-26").status_code == 200
    for bad in ("?mode=last7", "?mode=week&p=2026-10-12&p=2026-10-14&p=2026-10-26",
                "?mode=week&p=2026-10-12&p=2026-10-26&baseline=2026-10-05"):
        assert client.get("/compare" + bad).status_code == 400

def test_dashboard_flags_unpriced_models(root, configure):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", model="claude-mystery-9", output=5)])
    html = _client(configure(root)).get("/").get_data(as_text=True)
    assert "Unpriced models: claude-mystery-9" in html


def test_cli_ingest_reads_desktop_store_from_config(root, desktop, configure, env):
    from helpers import req
    root.write_transcript("p", "old", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000)])
    root.write_transcript("p", "new", [req("m2", "2026-10-30T02:00:00Z", output=1_000_000)])
    desktop.write_session("a", "new", priorCliSessionIds=["old"])
    configure(root)
    assert cli.main(["config", "set", "desktop_session_stores", str(desktop.path)],
                    env=env, now=NOW, out=io.StringIO()) == 0
    cfg = cli.load_config(env)
    assert cfg.desktop_stores == [desktop.path]
    conn = store.connect(cfg.store_path)
    out = io.StringIO()
    assert cli.main(["today"], env=env, now=NOW, out=out) == 0
    summaries = QueryService(store.connect(cfg.store_path), "UTC").session_summaries()
    assert [(s.session_id, s.requests) for s in summaries] == [("old", 2)]


def _spotlight_client(env):
    cfg = cli.load_config(env)
    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone, key_pattern=r"PROJ-\d+")
    return create_app(factory, lambda: NOW).test_client()


def test_spotlight_pages_edit_picked_sessions_and_compare(root, configure):
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T01:00:00Z", output=1_000_000)], title="PROJ-7 login")
    root.write_transcript("p", "s2", [req("m2", "2026-10-29T01:00:00Z", output=2_000_000)], title="PROJ-8 cart")
    root.write_transcript("p", "s3", [req("m3", "2026-10-29T01:00:00Z", output=1_000_000)], prompt="loose")
    client = _spotlight_client(configure(root))
    dash = client.get("/").get_data(as_text=True)
    assert 'id="top-spotlights"' in dash and "PROJ-7" in dash and "PROJ-8" in dash
    assert "never add them up" in client.get("/spotlights").get_data(as_text=True)
    page = client.get("/spotlights/PROJ-7")
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and 'id="spotlight-chart"' in html and "$20.00" in html
    assert "/static/spotlight.js" in html and client.get("/static/spotlight.js").status_code == 200
    # add a Picked session in the web UI, then remove it
    assert client.post("/spotlights/PROJ-7/add", data={"session": "s3"}).status_code == 302
    def sessions_table():
        return client.get("/spotlights/PROJ-7").get_data(as_text=True).split('id="spotlight-sessions"')[1].split("</table>")[0]
    assert "loose" in sessions_table()
    client.post("/spotlights/PROJ-7/remove", data={"session": "s3"})
    assert "loose" not in sessions_table()
    # create a Spotlight with a Picked session, reject a blank name, delete it
    assert client.post("/spotlights", data={"name": "carts", "session": ["s2"]}).status_code == 302
    assert "cart" in client.get("/spotlights/carts").get_data(as_text=True)
    assert client.post("/spotlights", data={"name": " "}).status_code == 400
    client.post("/spotlights/carts/delete")
    assert client.get("/spotlights/carts").status_code == 404
    cmp = client.get("/spotlights/compare?s=PROJ-7&s=PROJ-8")
    assert cmp.status_code == 200 and 'id="spotlight-compare-chart"' in cmp.get_data(as_text=True)
    assert client.get("/spotlights/compare?s=PROJ-7&s=nope").status_code == 404
    long_name = "Sprint 5 " + "very-long-spotlight-name-" * 4
    for n in (long_name, "short"):
        client.post("/spotlights", data={"name": n, "session": ["s2"]})
    page = client.get("/spotlights/compare", query_string={"s": [long_name, "short"]}).get_data(as_text=True)
    assert f'<td class="name-cell" title="{long_name}">' in page
    assert client.get("/spotlights/nope").status_code == 404


def test_breakdowns_page_filters_in_url_drill_down_and_csv(root, configure):
    root.write_transcript("proj-a", "s1", [
        req("m1", "2026-10-29T01:00:00Z", output=1_000_000, tools=("Bash",)),
        req("m2", "2026-10-29T02:00:00Z", model="claude-sonnet-5", output=1_000_000)])
    root.write_transcript("proj-b", "s2", [req("m3", "2026-10-30T01:00:00Z", output=500_000)])
    client = _client(configure(root))
    html = client.get("/breakdowns").get_data(as_text=True)
    assert 'id="cost-chart"' in html and "2026-10-29" in html
    assert "Breakdown by" in html and "Group by" not in html
    assert '<option value="date" selected>' in html  # defaults to Date
    assert 'value="session"' not in html  # session is not a breakdown
    for dim in ("Git branch", "Subagent type", "Cost component", "Cache event", "Tool", "Skill"):
        assert dim in html
    page = client.get("/breakdowns?group=model&project=proj-a&project=proj-b&tool=Bash")
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and "<strong>proj-a, proj-b</strong>" in html and 'title="Tool: Bash"' in html
    table = html.split('id="breakdowns-table"')[1]
    assert "claude-opus-5-5" in table and "claude-sonnet-5" not in table  # filtered slice
    # each row opens Sessions with its value added to the current filters
    link = re.search(r'href="(/sessions\?[^"]*model=claude-opus-5-5[^"]*)"', table).group(1)
    link = htmllib.unescape(link)
    assert "project=proj-a" in link and "project=proj-b" in link and "tool=Bash" in link
    drilled = client.get(link)
    assert drilled.status_code == 200
    csv_resp = client.get("/breakdowns.csv?group=project")
    assert csv_resp.mimetype == "text/csv"
    assert csv_resp.get_data(as_text=True).splitlines() == [
        "Project,Cost,Requests,Share %", "proj-a,30.00,2,75.0", "proj-b,10.00,1,25.0"]
    assert client.get("/breakdowns?group=session").status_code == 400
    assert client.get("/breakdowns?group=bogus").status_code == 400
    assert client.get("/breakdowns.csv?group=session").status_code == 400
    assert client.get("/breakdowns.csv?group=bogus").status_code == 400


def test_explore_is_gone_and_nav_shows_breakdowns(root, configure):
    root.write_transcript("proj-a", "s1", [req("m1", "2026-10-29T01:00:00Z", output=1_000_000)])
    client = _client(configure(root))
    for path in ("/explore", "/explore.csv", "/explore/requests", "/explore/requests?session=s1"):
        assert client.get(path).status_code == 404
    nav = client.get("/breakdowns").get_data(as_text=True).split('id="site-nav"')[1].split("</nav>")[0]
    assert ">Breakdowns<" in nav and "Explore" not in nav and 'href="/breakdowns"' in nav


def test_session_timeline_page_loads(root, configure):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=1000)])
    root.write_subagent_transcript("p", "s1", "a1", [req("m2", "2026-10-30T01:01:00Z", output=1000)])
    cfg = cli.load_config(configure(root))
    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    client = create_app(factory, lambda: NOW).test_client()
    page = client.get("/session/s1")
    assert page.status_code == 200 and 'id="timeline"' in page.get_data(as_text=True)
    assert "/session/s1/agent/a1" in page.get_data(as_text=True)
    assert client.get("/session/s1/agent/a1").status_code == 200
    assert client.get("/session/nope").status_code == 404


def test_cli_links_desktop_sessions_from_a_store_set_on_the_data_root(root, desktop, env):
    from pathlib import Path
    root.write_transcript("p", "old", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000)])
    root.write_transcript("p", "new", [req("m2", "2026-10-30T02:00:00Z", output=1_000_000)])
    desktop.write_session("a", "new", priorCliSessionIds=["old"])
    (Path(env["CLAUDE_COST_CONFIG_DIR"]) / "config.toml").write_text(
        f'timezone = "UTC"\ndata_roots = [{{ path = "{root.path}", '
        f'desktop_session_store = "{desktop.path}" }}]\n')
    cfg = cli.load_config(env)
    assert cfg.data_roots == [root.path] and cfg.desktop_stores == [desktop.path]
    assert cli.main(["today"], env=env, now=NOW, out=io.StringIO()) == 0
    summaries = QueryService(store.connect(cfg.store_path), "UTC").session_summaries()
    assert [(s.session_id, s.requests) for s in summaries] == [("old", 2)]
    # the global setting keeps working next to per-root ones
    other = Path(env["HOME"]) / "other-store"
    (Path(env["CLAUDE_COST_CONFIG_DIR"]) / "config.toml").write_text(
        f'data_roots = [{{ path = "{root.path}", desktop_session_store = "{desktop.path}" }}, "/x"]\n'
        f'desktop_session_stores = ["{other}"]\n')
    assert cli.load_config(env).desktop_stores == [other, desktop.path]


def test_today_and_since_use_the_configured_time_zone(root, configure):
    from datetime import datetime, timezone
    import pathlib
    env = configure(_root_with_spend(root))
    cfg_file = pathlib.Path(env["CLAUDE_COST_CONFIG_DIR"]) / "config.toml"
    cfg_file.write_text(cfg_file.read_text().replace('"UTC"', '"Pacific/Auckland"'))
    now = datetime(2026, 10, 30, 21, 30, tzinfo=timezone.utc)  # already Oct 31 in Auckland
    out = io.StringIO()
    assert cli.main(["today"], env=env, now=now, out=out) == 0
    assert out.getvalue().startswith("Today (2026-10-31)")
    cfg = cli.load_config(env)

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    page = create_app(factory, lambda: now).test_client().get("/").get_data(as_text=True)
    assert "2026-10-31" in page


def test_web_closes_its_connections_and_open_skips_the_schema(root, configure, tmp_path):
    import pytest
    import sqlite3
    env = configure(_root_with_spend(root))
    cfg = cli.load_config(env)
    opened = []

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        opened.append(conn)
        return QueryService(conn, cfg.timezone)
    assert create_app(factory, lambda: NOW).test_client().get("/").status_code == 200
    assert opened
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")  # closed when the request ended
    fresh = store.connect(tmp_path / "nothing.sqlite", schema=False)  # per-request open: no DDL
    assert fresh.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
