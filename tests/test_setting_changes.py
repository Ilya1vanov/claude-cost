"""Seams 1 and 2: Setting changes in the Session timeline (query service and Session page)."""
import json
import re

from helpers import req


def _changes(root, service_for, session="s1", agent=None):
    t = service_for(root).session_timeline(session, agent)
    return [(c.before_index, c.from_model, c.from_effort, c.to_model, c.to_effort)
            for c in t.setting_changes]


def _reqs(*specs):
    return [req(f"m{i}", f"2026-10-30T01:0{i}:00Z", input=10, output=5, **kw)
            for i, kw in enumerate(specs)]


def test_model_change_effort_change_and_both_are_each_one_setting_change(root, service_for):
    root.write_transcript("p", "s1", _reqs(
        dict(model="claude-opus-5-5", effort="high"),
        dict(model="claude-sonnet-5-5", effort="high"),   # model only
        dict(model="claude-sonnet-5-5", effort="low"),    # Effort only
        dict(model="claude-opus-5-5", effort="medium"),   # both
        dict(model="claude-opus-5-5", effort="medium")))  # neither
    assert _changes(root, service_for) == [
        (1, "claude-opus-5-5", "high", "claude-sonnet-5-5", "high"),
        (2, "claude-sonnet-5-5", "high", "claude-sonnet-5-5", "low"),
        (3, "claude-sonnet-5-5", "low", "claude-opus-5-5", "medium")]


def test_change_carries_the_time_of_the_request_it_happens_before(root, service_for):
    root.write_transcript("p", "s1", _reqs(
        dict(model="claude-opus-5-5"), dict(model="claude-sonnet-5-5")))
    (c,) = service_for(root).session_timeline("s1").setting_changes
    assert c.ts == service_for(root).session_timeline("s1").points[1].ts


def test_first_request_is_never_a_change(root, service_for):
    root.write_transcript("p", "s1", _reqs(dict(model="claude-opus-5-5", effort="high")))
    assert _changes(root, service_for) == []


def test_recorded_to_missing_effort_is_not_a_change(root, service_for):
    root.write_transcript("p", "s1", _reqs(
        dict(model="claude-opus-5-5", effort="high"),
        dict(model="claude-opus-5-5"),
        dict(model="claude-opus-5-5", effort="low")))
    assert _changes(root, service_for) == []


def test_missing_effort_with_a_model_change_is_a_change(root, service_for):
    root.write_transcript("p", "s1", _reqs(
        dict(model="claude-opus-5-5", effort="high"), dict(model="claude-sonnet-5-5")))
    assert _changes(root, service_for) == [
        (1, "claude-opus-5-5", "high", "claude-sonnet-5-5", "")]


def test_subagent_model_is_not_a_change_on_the_session_but_has_its_own_changes(root, service_for):
    root.write_transcript("p", "s1", _reqs(dict(model="claude-opus-5-5"), dict(model="claude-opus-5-5")))
    root.write_subagent_transcript("p", "s1", "a1", [
        req("x1", "2026-10-30T01:00:30Z", model="claude-haiku-4-5", input=10, output=5),
        req("x2", "2026-10-30T01:00:40Z", model="claude-sonnet-5-5", input=10, output=5)])
    assert _changes(root, service_for) == []
    assert _changes(root, service_for, agent="a1") == [
        (1, "claude-haiku-4-5", "", "claude-sonnet-5-5", "")]


def _page(root, tmp_path, url="/session/s1"):
    from claude_cost import ingest, store
    from claude_cost.query import QueryService
    from claude_cost.web import create_app
    from conftest import NOW
    conn = store.connect(tmp_path / "page.sqlite")
    ingest.ingest(conn, [root.path])
    client = create_app(lambda: QueryService(conn, "UTC"), lambda: NOW).test_client()
    return client.get(url).get_data(as_text=True)


def _chart(page):
    return json.loads(re.search(r'id="chart-data"[^>]*>(.*?)</script>', page, re.S)[1])


def _switching(root):
    root.write_transcript("p", "s1", _reqs(
        dict(model="claude-opus-5-5", effort="medium"),
        dict(model="claude-sonnet-5-5", effort="high")))


def test_events_table_has_a_row_per_change_with_from_to(root, tmp_path):
    _switching(root)
    events = _page(root, tmp_path).split('id="events"')[1].split("</table>")[0]
    assert "Setting change" in events
    assert "Opus 5.5 medium → Sonnet 5.5 high" in events
    assert "01:01:00" in events


def test_events_label_omits_a_missing_effort(root, tmp_path):
    root.write_transcript("p", "s1", _reqs(
        dict(model="claude-opus-5-5"), dict(model="claude-mystery-model")))
    events = _page(root, tmp_path).split('id="events"')[1].split("</table>")[0]
    assert "Opus 5.5 → claude-mystery-model" in events


def test_chart_data_carries_change_positions_and_labels(root, tmp_path):
    _switching(root)
    assert _chart(_page(root, tmp_path))["settingChanges"] == [
        {"index": 1, "label": "Opus 5.5 medium → Sonnet 5.5 high"}]


def test_subagent_page_shows_its_own_changes(root, tmp_path):
    root.write_transcript("p", "s1", _reqs(dict(model="claude-opus-5-5")))
    root.write_subagent_transcript("p", "s1", "a1", [
        req("x1", "2026-10-30T01:00:30Z", model="claude-haiku-4-5", input=10, output=5),
        req("x2", "2026-10-30T01:00:40Z", model="claude-sonnet-5-5", input=10, output=5)])
    page = _page(root, tmp_path, "/session/s1/agent/a1")
    assert _chart(page)["settingChanges"] == [{"index": 1, "label": "Haiku 4.5 → Sonnet 5.5"}]
    assert _chart(_page(root, tmp_path))["settingChanges"] == []


def test_legend_has_a_setting_change_entry(root, tmp_path):
    _switching(root)
    legend = _page(root, tmp_path).split('id="legend"')[1].split("</ul>")[0]
    assert "Setting change" in legend
