"""Seam 2: the Dashboard's Custom period from the URL, through the Flask test client."""
import pytest

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW  # Fri 2026-10-30
from helpers import req


@pytest.fixture
def client(root, configure):
    root.write_transcript("p", "s1", [req("m1", "2026-10-14T01:00:00Z", output=1_000_000)])
    root.write_transcript("p", "s2", [req("m2", "2026-10-09T01:00:00Z", output=2_000_000)])
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    return create_app(factory, lambda: NOW).test_client()


def _html(client, query):
    r = client.get("/?" + query)
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_custom_period_header_shows_dates_and_baseline_and_is_not_partial(client):
    html = _html(client, "period=custom&start=2026-10-12&end=2026-10-16")
    assert "2026-10-12 to 2026-10-16" in html and "2026-10-07 to 2026-10-11" in html
    assert "partial" not in html
    assert "$20.00" in html and "$40.00" in html   # current and baseline totals


def test_top_sessions_links_carry_the_custom_period(client):
    html = _html(client, "period=custom&start=2026-10-12&end=2026-10-16")
    assert "/session/s1?from=2026-10-12&amp;to=2026-10-16" in html


@pytest.mark.parametrize("query", [
    "period=custom",                                          # missing dates
    "period=custom&start=2026-10-12",                         # missing end
    "period=custom&start=2026-10-12&end=nope",                # malformed
    "period=custom&start=2026-10-16&end=2026-10-12",          # end before start
    "period=custom&start=2026-10-31&end=2026-11-02",          # start after today
    "period=custom&start=2026-10-20&end=2026-10-31",          # end after today
])
def test_bad_custom_parameters_are_400(client, query):
    assert client.get("/?" + query).status_code == 400


def test_no_period_is_still_last_7_days_with_include_today(client):
    html = _html(client, "")
    assert "2026-10-24 to 2026-10-30" in html
    assert "Include today" in html


def test_include_today_only_for_last7(client):
    assert "Include today" not in _html(client, "period=custom&start=2026-10-12&end=2026-10-16")
    assert "Include today" not in _html(client, "period=week")


def test_invalid_custom_period_is_rejected_before_any_ingest(root, configure):
    cfg = cli.load_config(configure(root))
    calls = []
    app = create_app(lambda: QueryService(store.connect(cfg.store_path), cfg.timezone),
                     lambda: NOW, refresh=lambda: calls.append(1))
    r = app.test_client().get("/?period=custom&start=2026-10-20&end=2026-10-31")
    assert r.status_code == 400
    assert calls == []
