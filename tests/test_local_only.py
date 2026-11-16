"""Seam: the dashboard answers only requests addressed to, and posted from, the local machine
(a DNS-rebinding or cross-site page must not read prompts or change Spotlights)."""
from datetime import datetime, timezone

import pytest

from claude_cost import store
from claude_cost.query import QueryService
from claude_cost.web import create_app

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(tmp_path):
    conn = store.connect(tmp_path / "page.sqlite")
    return create_app(lambda: QueryService(conn, "UTC"), lambda: NOW).test_client()


@pytest.mark.parametrize("host", ["localhost", "localhost:8787", "127.0.0.1:8787", "[::1]:8787"])
def test_local_host_names_are_served(client, host):
    assert client.get("/healthz", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8787", "127.0.0.1.evil.example"])
def test_a_foreign_host_name_is_refused(client, host):
    assert client.get("/healthz", headers={"Host": host}).status_code == 400


def test_a_post_from_a_foreign_origin_is_refused_and_a_local_one_allowed(client):
    assert client.post("/refresh", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/refresh", headers={"Origin": "http://localhost"}).status_code == 302
    assert client.post("/refresh").status_code == 302  # no Origin: not a browser cross-site post


def test_a_post_from_another_local_port_is_refused(client):
    """A page served by another local program (same host name, different port) is not this dashboard."""
    assert client.post("/refresh", headers={"Origin": "http://localhost:9999"}).status_code == 403


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_other_unsafe_methods_from_a_foreign_origin_are_refused(client, method):
    assert getattr(client, method)("/refresh", headers={"Origin": "https://evil.example"}).status_code == 403


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_browser_post_not_from_this_origin_is_refused(client, site):
    assert client.post("/refresh", headers={"Sec-Fetch-Site": site}).status_code == 403


def test_a_browser_post_from_this_origin_is_allowed(client):
    assert client.post("/refresh", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 302
