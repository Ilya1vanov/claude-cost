"""Incremental ingest, retention and freshness (ticket #6)."""
import io
import json
from datetime import date, timedelta

import pytest

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req

DAY = date(2026, 10, 30)


def total(svc):
    d = svc.daily_spend(DAY, DAY)[0]
    return d.cost, d.requests


def test_append_changes_totals_by_exactly_the_new_requests(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000)])
    assert total(service_for(root)) == (pytest.approx(20.0), 1)
    root.append_to_transcript("p", "s1", [req("m2", "2026-10-30T02:00:00Z", output=500_000)])
    assert total(service_for(root)) == (pytest.approx(30.0), 2)
    assert total(service_for(root)) == (pytest.approx(30.0), 2)  # no change when nothing new


def test_append_does_not_double_a_request_seen_in_another_file(root, service_for):
    # m1 exists in a resume copy; owner is the file named by its sessionId (s1).
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000)])
    root.write_transcript("p", "s2", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000,
                                          session_id="s1")])
    assert total(service_for(root)) == (pytest.approx(20.0), 1)
    root.append_to_transcript("p", "s2", [req("m2", "2026-10-30T03:00:00Z", output=500_000)])
    assert total(service_for(root)) == (pytest.approx(30.0), 2)


def test_streamed_message_split_across_runs_keeps_final_output(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000)])
    root.append_to_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=2_000_000)])
    assert total(service_for(root)) == (pytest.approx(40.0), 1)


def test_partial_last_line_is_read_once_complete(root, service_for):
    f = root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000)])
    line = json.dumps({"type": "assistant", "timestamp": "2026-10-30T02:00:00Z",
                       "sessionId": "s1", "message": {"id": "m2", "model": "claude-opus-5-5",
                       "usage": {"input_tokens": 0, "output_tokens": 500_000}}})
    with f.open("a") as fh:
        fh.write(line[:20])
    assert total(service_for(root)) == (pytest.approx(20.0), 1)
    with f.open("a") as fh:
        fh.write(line[20:] + "\n")
    assert total(service_for(root)) == (pytest.approx(30.0), 2)


def test_deleting_a_transcript_keeps_its_rows(root, service_for):
    f = root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000)])
    service_for(root)
    f.unlink()
    assert total(service_for(root)) == (pytest.approx(20.0), 1)


def test_rebuild_reingests_existing_files_and_keeps_deleted_history(root, tmp_path):
    gone = root.write_transcript("p", "gone", [req("g1", "2026-10-30T01:00:00Z", output=1_000_000)])
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T02:00:00Z", output=1_000_000)])
    conn = store.connect(tmp_path / "store.sqlite")
    ingest.ingest(conn, [root.path])
    gone.unlink()
    # tamper with a stored row; rebuild must recompute it from the transcript
    conn.execute("UPDATE requests SET cost_output = 0 WHERE message_id = 'm1'")
    conn.commit()
    assert total(QueryService(conn, "UTC"))[0] == pytest.approx(20.0)
    ingest.ingest(conn, [root.path], rebuild=True)
    assert total(QueryService(conn, "UTC")) == (pytest.approx(40.0), 2)


def test_truncated_transcript_is_read_afresh_without_losing_rows(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000),
                                      req("m2", "2026-10-30T02:00:00Z", output=1_000_000)])
    service_for(root)
    root.write_transcript("p", "s1", [req("m3", "2026-10-30T03:00:00Z", output=1)])
    assert total(service_for(root))[1] == 3


def _settings(root, days=None):
    if days is not None:
        (root.path / "settings.json").write_text(json.dumps({"cleanupPeriodDays": days}))


def test_first_run_warns_when_cleanup_period_below_60(root, configure):
    _settings(root, 30)
    env = configure(root)
    err = io.StringIO()
    cli.main(["ingest"], env=env, now=NOW, out=io.StringIO(), err=err)
    assert "cleanupPeriodDays is 30" in err.getvalue()
    err2 = io.StringIO()
    cli.main(["ingest"], env=env, now=NOW, out=io.StringIO(), err=err2)
    assert "cleanupPeriodDays" not in err2.getvalue()  # first run only


def test_no_warning_when_cleanup_period_is_60_or_more(root, configure):
    _settings(root, 99999)
    err = io.StringIO()
    cli.main(["ingest"], env=configure(root), now=NOW, out=io.StringIO(), err=err)
    assert "cleanupPeriodDays" not in err.getvalue()


def test_cli_ingest_and_rebuild(root, configure):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000)])
    env = configure(root)
    out = io.StringIO()
    assert cli.main(["ingest"], env=env, now=NOW, out=out) == 0
    assert "1 requests" in out.getvalue()
    out = io.StringIO()
    assert cli.main(["ingest", "--rebuild"], env=env, now=NOW, out=out) == 0
    assert "1 requests" in out.getvalue()


def _app(root, configure, clock):
    env = configure(root)
    cfg = cli.load_config(env)
    def refresh():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots, now=clock[0])
        conn.close()
    return create_app(lambda: QueryService(store.connect(cfg.store_path), "UTC"),
                      lambda: clock[0], refresh).test_client(), refresh


def test_page_load_refreshes_only_when_older_than_60s_and_refresh_button_forces(root, configure):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000)])
    clock = [NOW]
    client, _ = _app(root, configure, clock)
    assert "$20.00" in client.get("/").get_data(as_text=True)  # never ingested -> refresh
    root.append_to_transcript("p", "s1", [req("m2", "2026-10-30T02:00:00Z", output=1_000_000)])
    clock[0] = NOW + timedelta(seconds=30)
    assert "$20.00" in client.get("/").get_data(as_text=True)  # fresh: no refresh
    clock[0] = NOW + timedelta(seconds=61)
    assert "$40.00" in client.get("/").get_data(as_text=True)  # stale: refresh
    root.append_to_transcript("p", "s1", [req("m3", "2026-10-30T03:00:00Z", output=1_000_000)])
    page = client.get("/").get_data(as_text=True)
    assert "$40.00" in page and "Refresh" in page
    assert client.post("/refresh").status_code == 302
    assert "$60.00" in client.get("/").get_data(as_text=True)


def test_serve_ingests_on_start(root, configure):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000)])
    env = configure(root)
    cli.main(["serve"], env=env, now=NOW, out=io.StringIO(),
             run_server=lambda *a: None, open_browser=lambda u: None)
    cfg = cli.load_config(env)
    assert QueryService(store.connect(cfg.store_path), "UTC").last_ingest_epoch() == NOW.timestamp()


def test_owner_is_decided_across_runs_without_doubling(root, service_for):
    # A fork copy is ingested first; the original transcript shows up in a later run.
    root.write_transcript("p", "fork", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000,
                                            forked_from="orig")])
    assert total(service_for(root)) == (pytest.approx(20.0), 1)
    root.write_transcript("p", "orig", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000)])
    svc = service_for(root)
    assert total(svc) == (pytest.approx(20.0), 1)
    assert [s.cost for s in svc.session_summaries()] == [pytest.approx(20.0)]


def test_link_appended_later_groups_sessions(root, service_for):
    root.write_transcript("p", "a", [req("m1", "2026-10-30T01:00:00Z", output=1)])
    root.write_transcript("p", "b", [req("m2", "2026-10-30T02:00:00Z", output=1)])
    assert len(service_for(root).session_summaries()) == 2
    root.append_to_transcript("p", "a", [], raw=json.dumps(
        {"type": "continued-in", "continuedInSessionId": "b"}) + "\n")
    assert len(service_for(root).session_summaries()) == 1


def test_staleness_window_is_configurable(root, configure):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=1_000_000)])
    env = configure(root)
    cfg = cli.load_config(env)
    clock = [NOW]

    def refresh():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots, now=clock[0])
        conn.close()
    client = create_app(lambda: QueryService(store.connect(cfg.store_path), "UTC",
                                             thresholds={"refresh_stale_seconds": 5}),
                        lambda: clock[0], refresh).test_client()
    client.get("/")
    root.append_to_transcript("p", "s1", [req("m2", "2026-10-30T02:00:00Z", output=1_000_000)])
    clock[0] = NOW + timedelta(seconds=10)
    assert "$40.00" in client.get("/").get_data(as_text=True)   # 10 s > the 5 s window


def test_ingest_with_no_changes_does_not_rebuild_links_or_cache(root, desktop, tmp_path):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", write_1h=1_000)])
    desktop.write_session("d1", "s1")
    conn = store.connect(tmp_path / "s.sqlite")

    def run():
        ingest.ingest(conn, [root.path], desktop_stores=[desktop.path])

    def sentinel():
        conn.execute("UPDATE request_cache SET event = 'sentinel'")
        conn.commit()

    def event():
        return conn.execute("SELECT event FROM request_cache").fetchone()[0]
    run()
    sentinel()
    run()
    assert event() == "sentinel"  # nothing changed: classification was not rerun
    desktop.write_session("d2", "s1", priorCliSessionIds=["s0"])  # a Desktop store file changed
    run()
    assert event() == "warm"
    sentinel()
    root.append_to_transcript("p", "s1", [req("m2", "2026-10-30T02:00:00Z", write_1h=1_000)])
    run()
    assert event() != "sentinel"  # a transcript changed


def test_cli_ingest_stores_the_compact_analysis_for_the_configured_settings(root, configure, tmp_path):
    import dataclasses
    from claude_cost import compact, pricing
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=1_000)])
    cfg = cli.load_config(configure(root))
    custom = dataclasses.replace(cfg, thresholds={"compact_min_sample": 7, "compact_step": 5_000})
    cli._ingest(custom, NOW)
    conn = store.connect(cfg.store_path)
    table = pricing.build_table(cfg.prices)
    assert compact.stored(conn, compact.key(table, 7, 5_000)) is not None
    assert compact.stored(conn, compact.key(table, compact.MIN_SAMPLE, compact.STEP)) is None
    cli._ingest(dataclasses.replace(cfg, thresholds={}), NOW)  # missing settings fall back to defaults
    assert compact.stored(conn, compact.key(table, compact.MIN_SAMPLE, compact.STEP)) is not None
