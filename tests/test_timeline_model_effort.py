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
    return [r for r in json.loads(_page(root, tmp_path, url))["rows"] if r["kind"] in ("request", "subagent")]


def test_timeline_rows_show_short_model_and_effort(root, tmp_path):
    _session(root)
    r1, block, r2, r3 = _rows(root, tmp_path)
    assert (block["kind"], block["modelLabel"], block["effort"]) == ("subagent", "Haiku 4.5", "medium")
    assert (r1["modelLabel"], r1["effort"]) == ("Opus 5.5", "high")
    assert (r2["modelLabel"], r2["effort"]) == ("Sonnet 5.5", "")
    assert (r3["modelLabel"], r3["effort"]) == ("claude-mystery-model", "low")


def test_subagent_rows_show_the_same_fields(root, tmp_path):
    _session(root)
    (r1,) = _rows(root, tmp_path, "/api/session/s1/agent/a1/timeline")
    assert (r1["modelLabel"], r1["effort"]) == ("Haiku 4.5", "medium")


def test_model_family_names_the_color_token_of_a_model():
    from claude_cost.web import model_family
    assert [model_family(m) for m in (
        "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5-20251001", "claude-fable-5-1",
        "claude-3-5-sonnet-20241022", "claude-mystery-model", "")] == [
        "opus", "sonnet", "haiku", "fable", "unknown", "unknown", "unknown"]


def test_timeline_rows_carry_the_model_family(root, tmp_path):
    _session(root)
    r1, block, r2, r3 = _rows(root, tmp_path)
    assert [r["modelFamily"] for r in (r1, block, r2, r3)] == ["opus", "haiku", "sonnet", "unknown"]


def test_header_combinations_use_family_and_effort_colors(root, tmp_path):
    _session(root)
    html = _page(root, tmp_path)
    combos = html.split('id="combinations"')[1].split("</p>")[0]
    assert 'class="badge model-opus"' in combos and 'class="badge effort-high"' in combos
    assert 'class="badge model-unknown"' in combos and 'class="badge effort-low"' in combos
    assert 'class="badge model-sonnet"' in combos


def test_timeline_header_holds_the_sticky_setting_badge(root, tmp_path):
    _session(root)
    head = _page(root, tmp_path).split('class="tl-head"')[1].split("</div>")[0]
    assert 'id="timeline-setting"' in head


def test_stylesheet_defines_every_color_token_in_light_and_dark():
    from pathlib import Path
    import claude_cost
    css = (Path(claude_cost.__file__).parent / "static" / "app.css").read_text()
    dark = css.split("@media (prefers-color-scheme: dark)")[1]
    for token in ("opus", "sonnet", "haiku", "fable", "unknown"):
        for part in ("", "-fg"):
            assert f"--model-{token}{part}:" in css.split("@media (prefers-color-scheme: dark)")[0]
            assert f"--model-{token}{part}:" in dark
    for token in ("low", "medium", "high", "xhigh", "max"):
        for part in ("", "-fg"):
            assert f"--effort-{token}{part}:" in css.split("@media (prefers-color-scheme: dark)")[0]
            assert f"--effort-{token}{part}:" in dark
