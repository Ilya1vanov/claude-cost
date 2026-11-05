"""Seams 1 and 2: Subagent blocks in the Timeline (#89)."""
from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app
from conftest import NOW
from helpers import prompt_line, req


def _session(root):
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-30T01:00:10Z", output=100, text="launching"),
        req("m2", "2026-10-30T01:02:00Z", output=100, text="after"),
    ], extra_lines=[prompt_line("u1", "2026-10-30T01:00:00Z", "go")])
    root.write_subagent_transcript("p", "s1", "a1", [
        req("sa1", "2026-10-30T01:00:30Z", input=500, output=100, text="agent one"),
        req("sa2", "2026-10-30T01:00:40Z", output=200, text="agent two")],
        meta={"agentType": "Explore", "description": "look around"})
    root.write_subagent_transcript("p", "s1", "a2", [
        req("sb1", "2026-10-30T01:00:35Z", output=100, text="nested")],
        meta={"agentType": "general-purpose", "parentAgentId": "a1"})


def _client(root, configure):
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    return create_app(factory, lambda: NOW).test_client()


def test_a_subagent_is_one_row_where_it_started_with_its_own_rows(root, service_for):
    _session(root)
    rows = service_for(root).session_timeline("s1", with_rows=True).rows
    assert [r.kind for r in rows] == ["prompt", "request", "subagent", "request"]
    block = rows[2]
    assert (block.agent_id, block.agent_type, block.description) == ("a1", "Explore", "look around")
    assert block.requests == 2 and block.cost > 0 and block.peak_context > 0
    assert block.model and block.effort == ""
    assert [r.kind for r in block.rows] == ["request", "subagent", "request"]


def test_a_nested_subagent_sits_inside_its_parent_block_only(root, service_for):
    _session(root)
    rows = service_for(root).session_timeline("s1", with_rows=True).rows
    assert [r.agent_id for r in rows if r.kind == "subagent"] == ["a1"]
    (inner,) = [r for r in rows[2].rows if r.kind == "subagent"]
    assert inner.agent_id == "a2" and [r.text for r in inner.rows] == ["nested"]


def test_a_subagent_page_timeline_nests_its_own_children(root, service_for):
    _session(root)
    rows = service_for(root).session_timeline("s1", "a1", with_rows=True).rows
    assert [r.kind for r in rows] == ["request", "subagent", "request"]


def test_json_nests_subagent_rows(root, configure):
    _session(root)
    rows = _client(root, configure).get("/api/session/s1/timeline").get_json()["rows"]
    block = rows[2]
    assert block["kind"] == "subagent" and block["time"] == "2026-10-30 01:00:30"
    assert block["modelLabel"] and block["rows"][0]["modelLabel"] and block["rows"][0]["time"]
    assert block["rows"][1]["rows"][0]["text"] == "nested"


def test_the_subagent_page_is_a_timeline_of_its_stream_and_404s_for_unknown_agents(root, configure):
    _session(root)
    client = _client(root, configure)
    html = client.get("/session/s1/agent/a1").get_data(as_text=True)
    assert 'data-src="/api/session/s1/agent/a1/timeline"' in html
    rows = client.get("/api/session/s1/agent/a1/timeline").get_json()["rows"]
    assert [r["kind"] for r in rows] == ["request", "subagent", "request"]
    assert client.get("/api/session/s1/agent/nope/timeline").status_code == 404


def test_one_timeline_request_reads_each_transcript_once(root, service_for, monkeypatch):
    from claude_cost import timeline_rows
    _session(root)
    # the launching Session's transcript also holds a copy of a Subagent's request
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-30T01:00:10Z", output=100), req("m2", "2026-10-30T01:02:00Z", output=100),
        req("sa1", "2026-10-30T01:00:30Z", input=500, output=100, text="agent one"),
        req("sb1", "2026-10-30T01:00:35Z", output=100, text="nested")],
        extra_lines=[prompt_line("u1", "2026-10-30T01:00:00Z", "go")])
    svc, scanned = service_for(root), []
    real = timeline_rows.scan
    monkeypatch.setattr(timeline_rows, "scan", lambda path, *a: scanned.append(path) or real(path, *a))
    svc.session_timeline("s1", with_rows=True)
    assert scanned and len(scanned) == len(set(scanned))
