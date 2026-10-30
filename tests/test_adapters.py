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
