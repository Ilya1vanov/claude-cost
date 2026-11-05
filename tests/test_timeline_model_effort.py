"""Seams 1 and 2: model and Effort per request (query service timeline and Session page)."""
from helpers import req


def _session(root):
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-30T01:00:00Z", model="claude-opus-5-5", effort="high", input=10, output=5),
        req("m2", "2026-10-30T01:01:00Z", model="claude-sonnet-5-5", input=10, output=5),
        req("m3", "2026-10-30T01:02:00Z", model="claude-mystery-model", effort="low", input=10, output=5),
    ])
    root.write_subagent_transcript("p", "s1", "a1", [
        req("x1", "2026-10-30T01:00:30Z", model="claude-haiku-4-5-20251001", effort="medium",
            input=10, output=5)])


def _page(root, tmp_path, url="/session/s1"):
    from claude_cost import ingest, store
    from claude_cost.query import QueryService
    from claude_cost.web import create_app
    from conftest import NOW
    conn = store.connect(tmp_path / "page.sqlite")
    ingest.ingest(conn, [root.path])
    client = create_app(lambda: QueryService(conn, "UTC"), lambda: NOW).test_client()
    return client.get(url).get_data(as_text=True)


def test_timeline_points_carry_effort_empty_when_not_recorded(root, service_for):
    _session(root)
    t = service_for(root).session_timeline("s1")
    assert [p.effort for p in t.points] == ["high", "", "low"]
    sub = service_for(root).session_timeline("s1", "a1")
    assert [p.effort for p in sub.points] == ["medium"]


def _rows(root, tmp_path, url="/api/session/s1/timeline"):
    import json
    return json.loads(_page(root, tmp_path, url))["rows"]


def test_timeline_rows_show_short_model_and_effort(root, tmp_path):
    _session(root)
    r1, r2, r3 = _rows(root, tmp_path)
    assert (r1["modelLabel"], r1["effort"]) == ("Opus 5.5", "high")
    assert (r2["modelLabel"], r2["effort"]) == ("Sonnet 5.5", "")
    assert (r3["modelLabel"], r3["effort"]) == ("claude-mystery-model", "low")


def test_subagent_rows_show_the_same_fields(root, tmp_path):
    _session(root)
    (r1,) = _rows(root, tmp_path, "/api/session/s1/agent/a1/timeline")
    assert (r1["modelLabel"], r1["effort"]) == ("Haiku 4.5", "medium")
