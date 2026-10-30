"""Seams 1 and 2: compactions in the Session timeline (query service and Session page)."""
import re

from helpers import compact_line, req


def _session(root):
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-30T01:00:00Z", input=10, cache_read=0, write_5m=1000, output=100),
        req("m2", "2026-10-30T01:10:00Z", input=10, cache_read=1000, write_5m=500, output=100),
        req("m3", "2026-10-30T01:20:00Z", input=10, cache_read=1510, output=100),
    ], extra_lines=[compact_line("c1", "2026-10-30T01:05:00Z", 500_000, 8_000, "manual"),
                    compact_line("c2", "2026-10-30T01:15:00Z", 1_000, 800, "auto")])


def test_compactions_are_merged_into_the_request_sequence_by_time(root, service_for):
    _session(root)
    t = service_for(root).session_timeline("s1")
    kinds = [("compaction", x.trigger) if hasattr(x, "trigger") else ("request", x.message_id)
             for x in t.sequence]
    assert kinds == [("request", "m1"), ("compaction", "manual"), ("request", "m2"),
                     ("compaction", "auto"), ("request", "m3")]


def test_only_the_compaction_above_the_threshold_is_late(root, service_for):
    _session(root)
    t = service_for(root).session_timeline("s1")
    manual, auto = t.compactions
    assert (manual.trigger, manual.late) == ("manual", True)
    assert (auto.trigger, auto.late) == ("auto", False)


def _page(root, tmp_path):
    from claude_cost import ingest, store
    from claude_cost.query import QueryService
    from claude_cost.web import create_app
    from conftest import NOW
    conn = store.connect(tmp_path / "page.sqlite")
    ingest.ingest(conn, [root.path])
    client = create_app(lambda: QueryService(conn, "UTC"), lambda: NOW).test_client()
    return client.get("/session/s1").get_data(as_text=True)


def test_session_page_has_a_legend_with_one_entry_per_mark(root, tmp_path):
    _session(root)
    page = _page(root, tmp_path)
    legend = page.split('id="legend"')[1].split("</ul>")[0]
    for mark in ("Cold write", "after an idle gap", "Oversized context",
                 "Compact threshold", "Compaction"):
        assert mark in legend
    assert legend.count("<li") == 5


def test_requests_table_interleaves_compaction_rows_and_highlights_late_compact(root, tmp_path):
    _session(root)
    page = _page(root, tmp_path)
    rows = page.split('id="requests"')[1].split("</table>")[0]
    assert rows.index("01:00:00") < rows.index("Compaction (manual)") < rows.index("01:10:00") \
        < rows.index("Compaction (auto)") < rows.index("01:20:00")
    assert "500,000 to 8,000 tokens" in rows and "1,000 to 800 tokens" in rows
    assert rows.count("Late compact") == 1
    assert rows.index("Compaction (manual)") < rows.index("Late compact") < rows.index("01:10:00")


def test_chart_data_carries_compaction_positions(root, tmp_path):
    import json, re
    _session(root)
    page = _page(root, tmp_path)
    data = json.loads(re.search(r'id="chart-data" type="application/json">(.*?)</script>',
                                page, re.S).group(1))
    assert [(c["index"], c["trigger"], c["late"]) for c in data["compactions"]] == \
        [(1, "manual", True), (2, "auto", False)]


def test_purple_means_only_above_the_compact_threshold():
    from pathlib import Path
    css = (Path(__file__).parent.parent / "src" / "claude_cost" / "static" / "app.css").read_text()
    users = [l for l in css.splitlines()
             if re.search(r"var\(--late(-soft)?\)", l) and not l.strip().startswith("--late")]
    assert all(".over" in l or "swatch.line" in l for l in users), users  # Oversized context, threshold line


def test_late_compact_has_its_own_colour_not_the_cold_write_red():
    from pathlib import Path
    root = Path(__file__).parent.parent / "src" / "claude_cost" / "static"
    css = (root / "app.css").read_text()
    late = [l for l in css.splitlines() if "late-compact" in l and not l.strip().startswith("--")]
    assert late and not any("var(--up" in l for l in late), late
    assert 'theme.token("--late-compact")' in (root / "session.js").read_text()
