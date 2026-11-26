"""Timeline map (#149): the Session view carries the map element and script; the rows JSON has its prompts."""
import re
from pathlib import Path

import pytest

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import Advisor, compact_line, prompt_line, req, slash_command_line

MODULE = '<script type="module" src="/static/timeline_map.js"></script>'


@pytest.fixture
def client(root, configure):
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-30T01:00:10Z", output=100),
        req("m2", "2026-10-30T01:02:10Z", output=100)],
        extra_lines=[prompt_line("u1", "2026-10-30T01:00:00Z", "first line\nsecond line"),
                     slash_command_line("u2", "2026-10-30T01:02:00Z", "/review")])
    root.write_subagent_transcript("p", "s1", "a1", [req("sa1", "2026-10-30T01:00:30Z", output=100)],
                                   meta={"agentType": "Explore", "description": "look"})
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    return create_app(factory, lambda: NOW).test_client()


@pytest.mark.parametrize("url", ["/session/s1", "/session/s1/agent/a1"])
def test_pages_have_the_map_element_and_load_its_script_after_the_windowing_one(client, url):
    html = client.get(url).get_data(as_text=True)
    assert 'id="timeline-map"' in html and MODULE in html
    assert html.index("/static/timeline_window.js") < html.index(MODULE)
    assert 'id="timeline-rows"' in html.split('class="tl-main"')[1]
    assert client.get("/static/timeline_map.js").status_code == 200


def test_the_rows_json_carries_every_prompt_with_its_text_for_the_map(client):
    rows = client.get("/api/session/s1/timeline").get_json()["rows"]
    prompts = [r for r in rows if r["kind"] == "prompt"]
    assert [(p["text"].split("\n")[0], p["command"]) for p in prompts] == [
        ("first line", False), ("/review", True)]


# --- #150: every kind the map marks ---------------------------------------------------------------

STATIC = Path(__file__).resolve().parent.parent / "src" / "claude_cost" / "static"
ADV = Advisor(model="claude-opus-5-5", input=1000, output=100)


@pytest.fixture
def kinds_client(root, desktop, tmp_path):
    """One session with a cold write and Cold resume, a Late compact, a Setting change, an idle gap,
    a fork, a side session and an Advisor call (also inside a Subagent block)."""
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-29T01:00:00Z", input=900_000, effort="high", write_1h=100_000),
        req("m2", "2026-10-29T05:00:00Z", input=5_000, effort="low", write_1h=102_000),
        req("m3", "2026-10-29T05:00:10Z", input=5_000, effort="low", advisor=ADV),
        req("m4", "2026-10-29T05:00:20Z", input=5_000, effort="low")],
        extra_lines=[compact_line("c1", "2026-10-29T01:00:05Z", 900_000, 5_000, "auto")])
    root.write_subagent_transcript("p", "s1", "a1", [req("sa1", "2026-10-29T05:00:12Z", advisor=ADV)],
                                   meta={"agentType": "Explore", "description": "look"})
    root.write_transcript("p", "s9", [req("m1", "2026-10-29T01:00:00Z", input=900_000, effort="high",
                                          write_1h=100_000, forked_from="s1"),
                                      req("m9", "2026-10-29T06:00:00Z", output=10)])
    root.write_transcript("p", "s2", [req("m5", "2026-10-29T05:00:15Z", output=10)])
    desktop.write_session("a", "s1")
    desktop.write_session("b", "s2", spawnedFrom={"sessionId": "local_a", "sideSession": True})
    path = tmp_path / "kinds.sqlite"
    ingest.ingest(store.connect(path), [root.path], desktop_stores=[desktop.path.parent.parent])
    return create_app(lambda: QueryService(store.connect(path), "UTC"), lambda: NOW).test_client()


def test_the_rows_json_carries_every_kind_and_flag_the_map_reads(kinds_client):
    rows = kinds_client.get("/api/session/s1/timeline").get_json()["rows"]
    assert {"request", "setting_change", "compaction", "idle_gap", "child_session", "advisor_call",
            "subagent"} <= {r["kind"] for r in rows}
    assert any(r["kind"] == "compaction" and r["late"] for r in rows)
    assert any(r["kind"] == "request" and r["cold_write"] and r["cold_resume"] for r in rows)
    assert any(r["kind"] == "request" and not r["cold_write"] for r in rows)  # plain: unmarked
    assert {r["link_kind"] for r in rows if r["kind"] == "child_session"} == {"fork", "side"}
    sub = next(r for r in rows if r["kind"] == "subagent")
    assert "advisor_call" in {r["kind"] for r in sub["rows"]}
    fork = kinds_client.get("/api/session/s9/timeline").get_json()["rows"]
    assert fork[0]["kind"] == "forked_from"


def _static(name):
    return (STATIC / name).read_text()


def test_the_marks_use_the_chart_and_badge_tokens_and_the_fixed_segment_order():
    table = re.findall(r"^\s+(\w+): \{ token: \"(--[\w-]+)\", segment: (\d+)", _static("timeline_map.js"), re.M)
    assert [(k, t) for k, t, _ in table] == [
        ("prompt", "--accent"), ("compaction", "--compact"), ("late_compact", "--late-compact"),
        ("setting_change", "--setting"), ("idle_gap", "--warn"), ("link", "--muted"),
        ("cold", "--up"), ("advisor", "--advisor")]
    segment = {k: int(s) for k, _, s in table}
    assert segment["compaction"] == segment["late_compact"]
    order = ("prompt", "compaction", "setting_change", "idle_gap", "link", "cold", "advisor")
    assert [segment[k] for k in order] == list(range(7))


def test_the_advisor_token_is_defined_in_light_and_dark_and_the_tooltip_names_cold_kinds():
    assert len(re.findall(r"^\s+--advisor: #", _static("app.css"), re.M)) == 2
    js = _static("timeline_map.js")
    assert '"Cold resume"' in js and '"Cold write"' in js and "more" in js
