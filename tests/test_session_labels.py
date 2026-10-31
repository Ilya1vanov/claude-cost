"""Session labels everywhere: one QueryService source, rendered as linked labels with the id on hover."""
import html as htmllib

import pytest

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req

PROMPT = "Please investigate why the nightly export job keeps timing out on large accounts"
PROMPT_LABEL = PROMPT[:60]


@pytest.fixture
def setup(root, configure):
    fable = dict(model="claude-fable-5", output=1_000_000)
    root.write_transcript("p", "s-titled", [req("m1", "2026-10-29T01:00:00Z", **fable)],
                          title="Fix login bug", prompt=PROMPT)
    root.write_transcript("p", "s-prompt", [req("m2", "2026-10-29T02:00:00Z", **fable)], prompt=PROMPT)
    root.write_transcript("p", "s-bare", [req("m3", "2026-10-29T03:00:00Z", **fable)])
    root.write_subagent_transcript("p", "s-prompt", "a1", [req("sa1", "2026-10-29T02:01:00Z", **fable)],
                                   meta={"agentType": "Explore"})
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    svc = factory()
    svc.create_spotlight("S", ["s-titled", "s-prompt", "s-bare"])
    return svc, create_app(factory, lambda: NOW).test_client()


def test_session_labels_service_rule(setup):
    svc, _ = setup
    labels = svc.session_labels()
    assert labels["s-titled"] == "Fix login bug"
    assert labels["s-prompt"] == PROMPT_LABEL and len(labels["s-prompt"]) == 60
    assert labels["s-bare"] == "s-bare"


def _link(sid, label):
    return f'<a href="/session/{sid}" class="id" title="{sid}">{htmllib.escape(label)}</a>'


def _table(page, table_id):
    return page.split(f'id="{table_id}"')[1].split("</table>")[0]


@pytest.mark.parametrize("path", ["/spotlights/S", "/findings/fable-usage", "/explore?group=session",
                                  "/explore/requests"])
def test_pages_show_each_session_as_linked_label_with_id_on_hover(setup, path):
    _, client = setup
    page = client.get(path).get_data(as_text=True)
    assert _link("s-titled", "Fix login bug") in page
    assert _link("s-prompt", PROMPT_LABEL) in page
    assert _link("s-bare", "s-bare") in page


def test_spotlight_by_session_and_subagents_use_labels(setup):
    _, client = setup
    page = client.get("/spotlights/S").get_data(as_text=True)
    assert _link("s-titled", "Fix login bug") in _table(page, "spotlight-days")
    assert _link("s-prompt", PROMPT_LABEL) in _table(page, "spotlight-subagents")


def test_finding_detail_tables_use_labels(setup):
    _, client = setup
    page = client.get("/findings/fable-usage").get_data(as_text=True)
    for table in ("sessions", "subagents", "requests"):
        assert _link("s-prompt", PROMPT_LABEL) in _table(page, table)


def test_session_page_heading_and_links_use_labels(setup):
    _, client = setup
    page = client.get("/session/s-titled").get_data(as_text=True)
    assert '<span class="id" title="s-titled">Fix login bug</span>' in page
    sub = client.get("/session/s-prompt/agent/a1").get_data(as_text=True)
    assert _link("s-prompt", PROMPT_LABEL) in sub  # Parent session link
