"""Timeline windowing (#87): the Session view loads the vendored virtual-core as a module script."""
import re

import pytest

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req

SESSION_ID = "s-windowed"


@pytest.fixture
def client(root, configure):
    root.write_transcript("p", SESSION_ID, [req("m1", "2026-10-29T01:00:00Z", output=1_000_000)])
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    return create_app(factory, lambda: NOW).test_client()


def test_session_page_loads_windowing_module_script(client):
    html = client.get(f"/session/{SESSION_ID}").get_data(as_text=True)
    assert '<script type="module" src="/static/timeline_window.js"></script>' in html
    assert client.get("/static/timeline_window.js").status_code == 200


def test_vendored_virtual_core_is_self_contained_and_licensed(client):
    lib = client.get("/static/vendor/virtual-core.esm.js")
    code = lib.get_data(as_text=True)
    assert lib.status_code == 200
    assert "@tanstack/virtual-core@3.19.11" in code
    assert "Virtualizer" in code
    assert not re.search(r"^\s*import[\s{\"']", code, re.M) and "from\"" not in code.split("export{")[0]
    licence = client.get("/static/vendor/virtual-core-LICENSE").get_data(as_text=True)
    assert licence.startswith("MIT License") and "Tanner Linsley" in licence


def test_timeline_header_has_collapse_text_toggle(client):
    html = client.get(f"/session/{SESSION_ID}").get_data(as_text=True)
    head = html.split('class="tl-head"')[1].split("</div>")[0]
    assert 'id="timeline-collapse"' in head and "Collapse text" in head


def test_timeline_header_has_search_box(client):
    html = client.get(f"/session/{SESSION_ID}").get_data(as_text=True)
    head = html.split('class="tl-head"')[1].split("</div>")[0]
    assert 'id="timeline-search"' in head and 'id="timeline-search-count"' in head


def test_session_page_serves_the_timeline_box_and_its_rows_container(client):
    html = client.get(f"/session/{SESSION_ID}").get_data(as_text=True)
    assert 'id="timeline-box"' in html and 'id="timeline-rows"' in html


def test_session_script_loads_as_a_module_after_its_classic_dependencies(client):
    html = client.get(f"/session/{SESSION_ID}").get_data(as_text=True)
    module = '<script type="module" src="/static/session.js"></script>'
    assert module in html
    assert html.index("/static/theme.js") < html.index(module) < html.index("/static/timeline_window.js")


def test_session_page_has_no_inline_event_handlers(client):
    assert " onclick=" not in client.get(f"/session/{SESSION_ID}").get_data(as_text=True)
