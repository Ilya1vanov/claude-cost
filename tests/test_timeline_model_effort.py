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


def _rows(page):
    table = page.split('id="requests"')[1].split("</table>")[0]
    return [r for r in table.split("<tr")[1:] if "<td" in r]


def test_requests_table_shows_short_model_and_effort(root, tmp_path):
    _session(root)
    page = _page(root, tmp_path)
    table = page.split('id="requests"')[1].split("</table>")[0]
    assert "<th>Model</th>" in table and "<th>Effort</th>" in table
    r1, r2, r3 = _rows(page)
    assert "Opus 5.5" in r1 and "high" in r1
    assert "Sonnet 5.5" in r2 and "—" in r2
    assert "claude-mystery-model" in r3 and "low" in r3


def test_subagent_page_shows_the_same_columns(root, tmp_path):
    _session(root)
    page = _page(root, tmp_path, "/session/s1/agent/a1")
    (r1,) = _rows(page)
    assert "Haiku 4.5" in r1 and "medium" in r1


def test_compaction_rows_span_the_full_width(root, tmp_path):
    from helpers import compact_line
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-30T01:00:00Z", input=10, output=5),
        req("m2", "2026-10-30T01:10:00Z", input=10, output=5)],
        extra_lines=[compact_line("c1", "2026-10-30T01:05:00Z", 500, 80, "manual")])
    page = _page(root, tmp_path)
    assert 'colspan="5"' in page.split('id="requests"')[1]
