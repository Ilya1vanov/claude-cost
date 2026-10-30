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
