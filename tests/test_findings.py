"""Seam 1: findings (Drains and Opportunities) through the query service; seam 2: CLI and pages."""
import io
from datetime import date

import pytest

from claude_cost import cli, ingest, pricing, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req

# model claude-opus-5-5: write 1h $8/M; claude-fable-5: input $10/M, output $50/M


def _by_id(findings):
    return {f.id: f for f in findings}


def _service(tmp_path, root, thresholds=None):
    conn = store.connect(tmp_path / "findings.sqlite")
    ingest.ingest(conn, [root.path])
    return QueryService(conn, "UTC", thresholds=thresholds)


def _cold_session(root, resume_tokens=150_000, resume_read=0, project="p"):
    root.write_transcript(project, "s1", [
        req("first", "2026-10-30T10:00:00Z", write_1h=20_000),
        req("resume", "2026-10-30T13:00:00Z", write_1h=resume_tokens, cache_read=resume_read),
    ])


def test_cold_resume_attributed_and_avoidable(root, service_for):
    _cold_session(root)
    f = _by_id(service_for(root).findings())["cold-resume"]
    assert f.kind == "drain"
    assert f.attributed == pytest.approx(1.20)            # 150K x $8/M
    assert f.avoidable == pytest.approx(1.20 - 0.16)      # minus a fresh start at 20K (median first)
    assert [r.message_id for r in f.requests] == ["resume"]
    assert f.formula


def test_negative_avoidable_shows_as_zero(root, service_for):
    _cold_session(root, resume_tokens=5_000, resume_read=100_000)  # 105K context, tiny write
    f = _by_id(service_for(root).findings())["cold-resume"]
    assert f.attributed == pytest.approx(0.04 + 0.02)
    assert f.requests[0].avoidable < 0                   # raw figure kept for the drill-down
    assert f.avoidable == 0.0
    assert f.sessions[0].avoidable == 0.0


def test_cold_resume_needs_idle_and_context_thresholds(root, service_for):
    root.write_transcript("p", "s1", [
        req("a", "2026-10-30T10:00:00Z", write_5m=20_000),
        req("short-idle", "2026-10-30T10:30:00Z", write_5m=150_000),   # cold, 30 min < 60
        req("small", "2026-10-30T12:00:00Z", write_5m=50_000),         # cold, 90 min, 50K < 100K
    ])
    assert _by_id(service_for(root).findings())["cold-resume"].requests == ()


def test_thresholds_are_configurable(root, tmp_path):
    root.write_transcript("p", "s1", [
        req("a", "2026-10-30T10:00:00Z", write_5m=20_000),
        req("small", "2026-10-30T12:00:00Z", write_5m=50_000),
    ])
    f = _by_id(_service(tmp_path, root, {"cold_resume_min_context": 40_000}).findings())["cold-resume"]
    assert [r.message_id for r in f.requests] == ["small"]
    f = _by_id(_service(tmp_path, root, {"cold_resume_min_context": 40_000,
                                         "cold_resume_idle_minutes": 180}).findings())["cold-resume"]
    assert f.requests == ()


def test_fable_usage_avoidable_is_cost_minus_opus_55(root, service_for):
    root.write_transcript("p", "s1", [
        req("f", "2026-10-30T10:00:00Z", model="claude-fable-5", input=1_000_000, output=100_000),
        req("o", "2026-10-30T10:01:00Z", output=1_000_000),  # Opus: not covered
    ])
    f = _by_id(service_for(root).findings())["fable-usage"]
    assert f.kind == "opportunity"
    assert f.attributed == pytest.approx(10 + 5)          # $10 input + $5 output
    assert f.avoidable == pytest.approx(15 - (4 + 2))     # same tokens on Opus 5.5: $4 + $2
    assert [r.message_id for r in f.requests] == ["f"]


def test_findings_drill_down_to_sessions_and_since_filter(root, service_for):
    root.write_transcript("p", "s1", [
        req("old", "2026-10-01T10:00:00Z", model="claude-fable-5", output=1_000_000),
        req("new", "2026-10-29T10:00:00Z", model="claude-fable-5", output=1_000_000)])
    root.write_transcript("q", "s2", [req("other", "2026-10-30T10:00:00Z", model="claude-fable-5", output=100_000)])
    svc = service_for(root)
    f = _by_id(svc.findings())["fable-usage"]
    assert {s.session_id: s.requests for s in f.sessions} == {"s1": 2, "s2": 1}
    f = _by_id(svc.findings(since=date(2026, 10, 20)))["fable-usage"]
    assert {s.session_id: s.requests for s in f.sessions} == {"s1": 1, "s2": 1}
    assert svc.finding("fable-usage").id == "fable-usage"


def test_findings_are_listed_drains_first_with_unique_ids(root, service_for):
    root.write_transcript("p", "s1", [req("a", "2026-10-30T10:00:00Z", output=1)])
    found = service_for(root).findings()
    assert [f.kind for f in found] == sorted((f.kind for f in found), key=lambda k: k != "drain")
    assert len({f.id for f in found}) == len(found)


def test_cli_drains_prints_findings_not_additive_no_total(root, configure):
    _cold_session(root)
    out = io.StringIO()
    assert cli.main(["drains"], env=configure(root), now=NOW, out=out) == 0
    text = out.getvalue()
    assert "Cold resume: attributed $1.20, avoidable $1.04" in text
    assert "Fable usage" in text and "not additive" in text
    assert "avoid" in text and "total" not in text.lower()


def test_cli_drains_since_and_bad_since(root, configure):
    _cold_session(root)
    env = configure(root)
    out = io.StringIO()
    assert cli.main(["drains", "--since", "2026-10-31"], env=env, now=NOW, out=out) == 0
    assert "Cold resume: attributed $0.00" in out.getvalue()
    err = io.StringIO()
    assert cli.main(["drains", "--since", "soon"], env=env, now=NOW, out=io.StringIO(), err=err) == 2


def test_cli_config_threshold_applies(root, configure):
    _cold_session(root)
    env = configure(root)
    assert cli.main(["config", "set", "cold_resume_min_context", "200000"], env=env, out=io.StringIO()) == 0
    out = io.StringIO()
    cli.main(["drains"], env=env, now=NOW, out=out)
    assert "Cold resume: attributed $0.00" in out.getvalue()


def test_findings_pages_list_and_drill_down(root, configure):
    _cold_session(root)
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    client = create_app(factory, lambda: NOW).test_client()
    page = client.get("/findings").get_data(as_text=True)
    assert "Cold resume" in page and "$1.04" in page and "not additive" in page
    detail = client.get("/findings/cold-resume").get_data(as_text=True)
    assert "resume" in detail and "s1" in detail and "avoidable" in detail.lower()
    assert client.get("/findings/nope").status_code == 404


def test_findings_list_shows_only_title_and_totals(root, configure):
    _cold_session(root)
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    client = create_app(factory, lambda: NOW).test_client()
    f = next(f for f in factory().findings() if f.id == "cold-resume")
    page = client.get("/findings").get_data(as_text=True)
    assert f.title in page
    for text in (f.description, f.formula, f.detail):
        assert text and text not in page


# ---- dashboard top-N findings card (story 44) ----

def _fable_and_cold(root):
    root.write_transcript("p", "s1", [
        req("f1", "2026-10-30T10:00:00Z", model="claude-fable-5", output=1_000_000),  # $50
        req("first", "2026-10-30T10:01:00Z", write_1h=20_000),
        req("resume", "2026-10-30T13:00:00Z", write_1h=150_000),                      # $1.20 cold
    ])


def test_top_findings_ranked_by_attributed_spend_and_limited_to_top_n(root, service_for):
    _fable_and_cold(root)
    svc = service_for(root)
    top = svc.top_findings("last7", NOW, top_n=5)
    assert [f.id for f in top] == ["fable-usage", "cold-resume"]
    assert [f.id for f in svc.top_findings("last7", NOW, top_n=1)] == ["fable-usage"]


def test_top_findings_only_count_requests_in_the_period(root, service_for):
    root.write_transcript("p", "s1", [
        req("old", "2026-09-01T10:00:00Z", model="claude-fable-5", output=1_000_000)])
    assert service_for(root).top_findings("last7", NOW) == []


def test_dashboard_shows_top_findings_card_using_config_top_n(root, configure):
    _fable_and_cold(root)
    env = configure(root)
    from pathlib import Path
    with (Path(env["CLAUDE_COST_CONFIG_DIR"]) / "config.toml").open("a") as f:
        f.write("top_n = 1\n")
    out = io.StringIO()
    cfg = cli.load_config(env)

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    html = create_app(factory, lambda: NOW, top_n=cfg.top_n).test_client().get("/").get_data(as_text=True)
    assert 'id="top-findings"' in html and "Fable usage" in html and "Cold resume" not in html


def test_fable_comparison_model_is_configurable(root, service_for):
    root.write_transcript("p", "s1", [
        req("f", "2026-10-30T10:00:00Z", model="claude-fable-5", input=1_000_000, output=100_000)])
    f = _by_id(service_for(root, compare_model="claude-sonnet-5-5").findings())["fable-usage"]
    assert f.avoidable == pytest.approx(15 - (2 + 1))     # Sonnet 5.5: $2 input + $10/M output


def test_web_since_accepts_days_back_like_the_cli(root, service_for):
    root.write_transcript("p", "s1", [
        req("old", "2026-10-01T10:00:00Z", model="claude-fable-5", output=1_000_000),
        req("new", "2026-10-29T10:00:00Z", model="claude-fable-5", output=1_000_000)])
    client = create_app(lambda: service_for(root), lambda: NOW).test_client()
    page = client.get("/findings/fable-usage?since=7d").get_data(as_text=True)
    assert "2026-10-23" in page   # NOW is 2026-10-30
    assert client.get("/findings?since=garbage").status_code == 400


def test_cold_resume_gap_is_per_stream_not_per_session(root, service_for):
    """A Subagent running during the idle period does not hide the main stream's Cold resume."""
    _cold_session(root)  # main: 10:00 write, 13:00 cold resume
    root.write_subagent_transcript("p", "s1", "a1", [req("sub", "2026-10-30T12:30:00Z", write_1h=1_000)])
    f = _by_id(service_for(root).findings())["cold-resume"]
    assert [r.message_id for r in f.requests] == ["resume"]


# --- findings load only Period Requests; Stream facts come from ingest ---

def test_cold_resume_detects_a_cold_write_whose_previous_request_precedes_the_period(root, service_for):
    _cold_session(root)  # 10:00 on 10-30, resume at 13:00 on 10-30 (same day as the Period below)
    root.write_transcript("p", "s2", [
        req("old", "2026-09-01T10:00:00Z", write_1h=20_000),
        req("back", "2026-10-30T09:00:00Z", write_1h=150_000)])
    svc = service_for(root)
    f = _by_id(svc.findings(date(2026, 10, 30), date(2026, 10, 30)))["cold-resume"]
    assert {r.message_id for r in f.requests} == {"resume", "back"}


def test_findings_context_holds_only_period_requests(root, service_for):
    from claude_cost import findings as findings_mod
    _cold_session(root)
    svc = service_for(root)
    rows = findings_mod.load_rows(svc._conn, None, 1e18, None)
    assert rows == []
    assert [r.message_id for r in findings_mod.load_rows(svc._conn, None, None, None)] == ["first", "resume"]


@pytest.fixture
def classify_calls(monkeypatch):
    from claude_cost import cache
    calls = []
    real = cache.classify_all
    monkeypatch.setattr(cache, "classify_all", lambda conn: (calls.append(1), real(conn))[1])
    return calls


def test_reingest_recomputes_stream_facts_only_when_inputs_changed(root, tmp_path, classify_calls):
    _cold_session(root)
    conn = store.connect(tmp_path / "s.sqlite")
    ingest.ingest(conn, [root.path])
    assert len(classify_calls) == 1
    ingest.ingest(conn, [root.path])
    assert len(classify_calls) == 1
    root.write_transcript("p", "s9", [req("n1", "2026-10-30T11:00:00Z", write_1h=1_000)])
    ingest.ingest(conn, [root.path])
    assert len(classify_calls) == 2


def test_pre_change_store_gets_stream_facts_at_the_next_ingest(root, tmp_path, classify_calls):
    _cold_session(root)
    conn = store.connect(tmp_path / "s.sqlite")
    ingest.ingest(conn, [root.path])
    with conn:
        conn.execute("DELETE FROM request_stream")
    ingest.ingest(conn, [root.path])
    assert len(classify_calls) == 2
    assert conn.execute("SELECT COUNT(*) FROM request_stream").fetchone()[0] == 2


def test_a_requests_agent_id_and_stream_facts_come_from_the_same_stream(root, service_for):
    from claude_cost import findings as findings_mod
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T10:00:00Z", input=1_000),
                                      req("m2", "2026-10-30T10:01:00Z", input=2_000)])
    root.write_subagent_transcript("p", "s1", "a1", [req("a1-1", "2026-10-30T10:00:30Z", input=50_000),
                                                     req("a1-2", "2026-10-30T10:02:00Z", input=60_000)])
    svc = service_for(root)
    with svc._conn:  # the Request's Stream is the one cache classification stored: its surface
        svc._conn.execute("DELETE FROM subagent_files")
    rows = {r.message_id: r for r in findings_mod.load_rows(svc._conn, None, None, None)}
    assert rows["a1-1"].agent_id == "a1" and rows["a1-1"].gap is None
    assert rows["a1-1"].stream_first_context == rows["a1-2"].stream_first_context == 50_000
    assert rows["a1-2"].gap == pytest.approx(90.0)
    assert rows["m2"].agent_id is None and rows["m2"].stream_first_context == 1_000


def test_store_missing_the_median_baseline_gets_it_at_the_next_ingest(root, tmp_path, classify_calls):
    from claude_cost import cache
    _cold_session(root)
    conn = store.connect(tmp_path / "s.sqlite")
    ingest.ingest(conn, [root.path])
    with conn:
        conn.execute("DELETE FROM meta WHERE key = ?", (cache.MEDIAN_FIRST_META,))
    ingest.ingest(conn, [root.path])
    assert len(classify_calls) == 1  # only the missing baseline is computed, no Stream is reclassified
    assert cache.median_first_context(conn) == 20_000


def test_store_missing_the_bare_subagent_baseline_gets_only_that_row_at_the_next_ingest(
        root, tmp_path, classify_calls):
    from claude_cost import cache
    _cold_session(root)
    conn = store.connect(tmp_path / "s.sqlite")
    ingest.ingest(conn, [root.path])
    with conn:
        conn.execute("DELETE FROM meta WHERE key = ?", (cache.BARE_SUBAGENT_META,))
    ingest.ingest(conn, [root.path])
    assert len(classify_calls) == 1
    assert conn.execute("SELECT 1 FROM meta WHERE key = ?", (cache.BARE_SUBAGENT_META,)).fetchone()


def test_findings_on_a_store_from_before_the_change_work_after_the_cli_ingests(root, configure, tmp_path):
    """The CLI ingests (creating the new tables and indexes) before it opens the store for reading."""
    _cold_session(root)
    env = configure(root)
    conn = store.connect(tmp_path / "data" / "store.sqlite")
    ingest.ingest(conn, [root.path])
    with conn:
        conn.execute("DROP TABLE request_stream")
        conn.execute("DROP INDEX tool_uses_ts")
        conn.execute("DELETE FROM meta WHERE key = 'median_first_context'")
    conn.close()
    out = io.StringIO()
    assert cli.main(["drains"], env=env, now=NOW, out=out, err=io.StringIO()) == 0
    assert "Cold resume: attributed $1.20, avoidable $1.04" in out.getvalue()
    check = store.connect(tmp_path / "data" / "store.sqlite", schema=False)
    assert check.execute("SELECT COUNT(*) FROM request_stream").fetchone()[0] == 2
    assert "tool_uses_ts" in {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def test_cold_resume_reports_nothing_when_the_median_baseline_is_missing(root, tmp_path):
    from claude_cost import cache
    _cold_session(root)
    svc = _service(tmp_path, root)
    with svc._conn:
        svc._conn.execute("DELETE FROM meta WHERE key = ?", (cache.MEDIAN_FIRST_META,))
    assert cache.median_first_context(svc._conn) is None
    f = _by_id(svc.findings())["cold-resume"]
    assert f.requests == () and f.attributed == 0
    assert "unavailable" in f.detail


def test_a_request_without_stream_facts_gets_them_at_the_next_ingest(root, tmp_path, classify_calls):
    _cold_session(root)
    conn = store.connect(tmp_path / "s.sqlite")
    ingest.ingest(conn, [root.path])
    with conn:  # the Request keeps its cache event but lost its Stream facts
        conn.execute("DELETE FROM request_stream WHERE message_id = 'resume'")
    ingest.ingest(conn, [root.path])
    assert len(classify_calls) == 2
    row = conn.execute("SELECT gap, first_context FROM request_stream WHERE message_id = 'resume'").fetchone()
    assert (row["gap"], row["first_context"]) == (10_800, 20_000)


def _classified_rows(conn):
    return ([tuple(r) for r in conn.execute("SELECT * FROM request_cache ORDER BY message_id")],
            [tuple(r) for r in conn.execute("SELECT * FROM request_stream ORDER BY message_id")],
            conn.execute("SELECT value FROM meta WHERE key = 'median_first_context'").fetchone()[0])


def test_incremental_classification_equals_a_full_recompute(root, tmp_path):
    from claude_cost import cache
    _cold_session(root)
    root.write_transcript("q", "s2", [req("q1", "2026-10-30T09:00:00Z", write_1h=50_000),
                                      req("q2", "2026-10-30T09:02:00Z", cache_read=50_000, write_1h=1_000)])
    root.write_subagent_transcript("p", "s1", "a1", [req("a1-1", "2026-10-30T10:00:30Z", input=70_000)])
    conn = store.connect(tmp_path / "s.sqlite")
    ingest.ingest(conn, [root.path])
    # a later Request in an existing Stream, an earlier one in another, and a new Session
    root.write_transcript("p", "s1b", [req("late", "2026-10-30T20:00:00Z", write_1h=10_000, session_id="s1")])
    root.write_transcript("q", "s2b", [req("q0", "2026-10-30T08:00:00Z", write_1h=999_000, session_id="s2")])
    root.write_transcript("r", "s3", [req("r1", "2026-10-30T11:00:00Z", write_1h=7_000)])
    ingest.ingest(conn, [root.path])
    incremental = _classified_rows(conn)
    with conn:
        conn.execute("DELETE FROM request_cache")
        conn.execute("DELETE FROM request_stream")
        cache.classify_all(conn)
    assert incremental == _classified_rows(conn)
    assert len(incremental[1]) == 8


def test_ingest_classifies_only_the_streams_that_gained_requests(root, tmp_path):
    _cold_session(root)
    root.write_transcript("q", "s2", [req("q1", "2026-10-30T09:00:00Z", write_1h=50_000)])
    conn = store.connect(tmp_path / "s.sqlite")
    ingest.ingest(conn, [root.path])
    with conn:  # a marker only a rewrite of the other Stream would erase
        conn.execute("UPDATE request_stream SET gap = 12345 WHERE message_id = 'q1'")
    root.write_transcript("p", "s1b", [req("n1", "2026-10-30T13:05:00Z", cache_read=150_000, session_id="s1")])
    ingest.ingest(conn, [root.path])
    gaps = dict(conn.execute("SELECT message_id, gap FROM request_stream").fetchall())
    assert gaps["q1"] == 12345 and gaps["n1"] == 300
