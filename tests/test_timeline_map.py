"""Timeline map (#149): the Session view carries the map element and script; the rows JSON has its prompts."""
import pytest

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import prompt_line, req, slash_command_line

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
