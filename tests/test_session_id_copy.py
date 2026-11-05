"""Session view header: full id with a copy control (Session and Subagent pages)."""
import pytest

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req

SESSION_ID = "s-0123456789abcdef-long-session-id"
AGENT_ID = "agent-0123456789abcdef-long-agent-id"


@pytest.fixture
def client(root, configure):
    fable = dict(model="claude-fable-5", output=1_000_000)
    root.write_transcript("p", SESSION_ID, [req("m1", "2026-10-29T01:00:00Z", **fable)])
    root.write_subagent_transcript("p", SESSION_ID, AGENT_ID, [req("sa1", "2026-10-29T01:01:00Z", **fable)],
                                   meta={"agentType": "Explore"})
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    return create_app(factory, lambda: NOW).test_client()


def _head(page):
    return page.split('class="page-head"', 1)[1].split("</div>", 1)[0]


def test_session_page_shows_full_id_with_copy_button(client):
    head = _head(client.get(f"/session/{SESSION_ID}").get_data(as_text=True))
    assert f'<code class="full-id">{SESSION_ID}</code>' in head
    assert f'data-copy="{SESSION_ID}"' in head


def test_subagent_page_shows_full_agent_id_with_copy_button(client):
    head = _head(client.get(f"/session/{SESSION_ID}/agent/{AGENT_ID}").get_data(as_text=True))
    assert f'<code class="full-id">{AGENT_ID}</code>' in head
    assert f'data-copy="{AGENT_ID}"' in head
