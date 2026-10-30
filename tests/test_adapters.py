"""Seam 2: adapter smoke tests. No logic asserted beyond shape."""
import io

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
             run_server=lambda app, host, port: seen.update(host=host, port=port),
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
    assert html.count("sess-") == 2


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


def test_spotlight_pages_edit_pins_and_compare(root, configure):
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
    # pin a Session in the web UI, then unpin it
    assert client.post("/spotlights/PROJ-7/pin", data={"session": "s3"}).status_code == 302
    def sessions_table():
        return client.get("/spotlights/PROJ-7").get_data(as_text=True).split('id="spotlight-sessions"')[1].split("</table>")[0]
    assert "loose" in sessions_table()
    client.post("/spotlights/PROJ-7/unpin", data={"session": "s3"})
    assert "loose" not in sessions_table()
    # create a rule Spotlight, reject a bad rule, delete it
    assert client.post("/spotlights", data={"name": "carts", "rule": "cart"}).status_code == 302
    assert "PROJ-8" in client.get("/spotlights/carts").get_data(as_text=True)
    assert client.post("/spotlights", data={"name": "bad", "rule": "("}).status_code == 400
    client.post("/spotlights/carts/delete")
    assert client.get("/spotlights/carts").status_code == 404
    cmp = client.get("/spotlights/compare?s=PROJ-7&s=PROJ-8")
    assert cmp.status_code == 200 and 'id="spotlight-compare-chart"' in cmp.get_data(as_text=True)
    assert client.get("/spotlights/compare?s=PROJ-7&s=nope").status_code == 404
    assert client.get("/spotlights/nope").status_code == 404
