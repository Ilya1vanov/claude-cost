"""Seams 1 and 2: model + Effort combinations of a timeline (query service and Session page header)."""
from helpers import req
from test_timeline_model_effort import _page


def _session(root):
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-30T01:00:00Z", model="claude-opus-5-5", effort="high", input=10, output=5),
        req("m2", "2026-10-30T01:01:00Z", model="claude-sonnet-5-5", effort="medium", input=10, output=5),
        req("m3", "2026-10-30T01:02:00Z", model="claude-opus-5-5", effort="high", input=10, output=5),
        req("m4", "2026-10-30T01:03:00Z", model="claude-sonnet-5-5", input=10, output=5),
    ])
    root.write_subagent_transcript("p", "s1", "a1", [
        req("x1", "2026-10-30T01:00:30Z", model="claude-haiku-4-5-20251001", effort="low", input=10, output=5),
        req("x2", "2026-10-30T01:00:40Z", model="claude-haiku-4-5-20251001", effort="low", input=10, output=5)])


def _header(page):
    import re
    return re.sub(r"<[^>]+>", "", page.split('id="combinations"')[1].split("</p>")[0])  # the visible text


def test_timeline_combinations_in_order_of_first_use_own_requests_only(root, service_for):
    _session(root)
    t = service_for(root).session_timeline("s1")
    assert [(c.model, c.effort, c.requests) for c in t.combinations] == [
        ("claude-opus-5-5", "high", 2), ("claude-sonnet-5-5", "medium", 1), ("claude-sonnet-5-5", "", 1)]
    by_id = {p.message_id: p.cost for p in t.points}
    assert t.combinations[0].cost == by_id["m1"] + by_id["m3"]
    sub = service_for(root).session_timeline("s1", "a1")
    assert [(c.model, c.effort, c.requests) for c in sub.combinations] == [("claude-haiku-4-5-20251001", "low", 2)]


def test_header_lists_several_combinations_with_counts_and_costs(root, tmp_path):
    _session(root)
    h = _header(_page(root, tmp_path))
    assert "Opus 5.5 high: 2 requests, $" in h
    assert "Sonnet 5.5 medium: 1 request, $" in h
    assert "Sonnet 5.5: 1 request" in h
    assert h.index("Opus 5.5") < h.index("Sonnet 5.5 medium") < h.index("Sonnet 5.5:")
    assert "Haiku" not in h


def test_header_single_combination_has_no_counts(root, tmp_path):
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-30T01:00:00Z", model="claude-opus-5-5", effort="high", input=10, output=5),
        req("m2", "2026-10-30T01:01:00Z", model="claude-opus-5-5", effort="high", input=10, output=5)])
    h = _header(_page(root, tmp_path))
    assert "Opus 5.5 high" in h and "requests" not in h and "$" not in h


def test_header_single_combination_without_effort_is_the_model_alone(root, tmp_path):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", model="claude-opus-5-5", input=10, output=5)])
    h = _header(_page(root, tmp_path))
    assert "Opus 5.5" in h and "·" not in h


def test_subagent_page_header_covers_its_own_requests(root, tmp_path):
    _session(root)
    h = _header(_page(root, tmp_path, "/session/s1/agent/a1"))
    assert "Haiku 4.5 low" in h and "Opus" not in h
