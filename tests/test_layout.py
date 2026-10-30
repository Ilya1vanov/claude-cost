"""Seam: every page shares one layout: the stylesheet, the nav, no inline styles, no network."""
import pytest

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req

PAGES = (
    "/",
    "/compare",
    "/compare?mode=week&p=2026-10-19&p=2026-10-26&baseline=2026-10-19",
    "/explore",
    "/explore/requests?session=s1",
    "/findings",
    "/findings/fable-usage",
    "/compact",
    "/spotlights",
    "/spotlights/PROJ-7",
    "/spotlights/compare?s=PROJ-7&s=PROJ-8",
    "/session/s1",
)


@pytest.fixture
def client(root, configure):
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T01:00:00Z", model="claude-fable-5",
                                          output=1_000_000)], title="PROJ-7 login")
    root.write_transcript("p", "s2", [req("m2", "2026-10-22T01:00:00Z", output=1_000_000)],
                          title="PROJ-8 cart")
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone, key_pattern=r"PROJ-\d+")
    return create_app(factory, lambda: NOW).test_client()


@pytest.mark.parametrize("path", PAGES)
def test_page_uses_shared_layout(client, path):
    page = client.get(path)
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "/static/app.css" in html and 'id="site-nav"' in html
    assert " style=" not in html
    assert "http://" not in html and "https://" not in html  # no CDN, no network


def test_stylesheet_is_served(client):
    assert client.get("/static/app.css").status_code == 200
