"""Seam 2: the Dashboard's period trigger and rail, through the Flask test client."""
import pytest

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW  # Fri 2026-10-30
from helpers import req


def _client(root, configure, first_day):
    root.write_transcript("p", "s1", [req("m1", f"{first_day}T01:00:00Z", output=1_000_000)])
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    return create_app(factory, lambda: NOW).test_client()


@pytest.fixture
def client(root, configure):
    return _client(root, configure, "2026-10-09")


def _html(client, query=""):
    r = client.get("/?" + query)
    assert r.status_code == 200
    return r.get_data(as_text=True)


def _link(start, end):
    return f'href="/?period=custom&amp;start={start}&amp;end={end}"'


@pytest.mark.parametrize("query, label", [
    ("", "Last 7 days · 24 Oct – 30 Oct"),
    ("period=last7&include_today=0", "Last 7 days · 23 Oct – 29 Oct"),
    ("period=week", "Calendar week · 26 Oct – 30 Oct"),
    ("period=month", "Calendar month · 1 Oct – 30 Oct"),
    ("period=custom&start=2026-10-03&end=2026-10-16", "Custom · 3 Oct – 16 Oct"),
])
def test_trigger_shows_the_period_name_and_dates(client, query, label):
    assert f'class="period-trigger"' in _html(client, query)
    assert label in _html(client, query)


def test_rail_links_load_the_three_existing_kinds(client):
    html = _html(client)
    for p in ("last7", "week", "month"):
        assert f'href="/?period={p}"' in html


def test_shortcuts_are_custom_periods_with_fixed_dates_ending_today(client):
    html = _html(client)
    assert _link("2026-10-17", "2026-10-30") in html   # Last 14 days
    assert _link("2026-10-01", "2026-10-30") in html   # Last 30 days
    assert _link("2026-08-02", "2026-10-30") in html   # Last 90 days


def test_all_time_and_year_to_date_start_at_the_first_request_day(client):
    html = _html(client)
    assert _link("2026-10-09", "2026-10-30") in html   # All time
    assert html.count(_link("2026-10-09", "2026-10-30")) == 2   # and Year to date (1 Jan is earlier)


def test_year_to_date_starts_on_1_january_when_history_is_older(root, configure):
    html = _html(_client(root, configure, "2025-03-04"))
    assert _link("2025-03-04", "2026-10-30") in html   # All time
    assert _link("2026-01-01", "2026-10-30") in html   # Year to date


def test_empty_store_dashboard_still_renders_with_shortcuts(root, configure):
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        return QueryService(conn, cfg.timezone)
    html = _html(create_app(factory, lambda: NOW).test_client())
    assert _link("2026-10-30", "2026-10-30") in html   # All time collapses to today


def test_include_today_only_beside_the_trigger_for_last7(client):
    assert "Include today" in _html(client)
    for q in ("period=week", "period=month", "period=custom&start=2026-10-12&end=2026-10-16"):
        assert "Include today" not in _html(client, q)


@pytest.mark.parametrize("query, start, end", [
    ("", "2026-10-24", "2026-10-30"),
    ("period=month", "2026-10-01", "2026-10-30"),
    ("period=custom&start=2026-10-03&end=2026-10-16", "2026-10-03", "2026-10-16"),
])
def test_popover_hands_the_calendar_its_bounds_and_the_current_period(client, query, start, end):
    html = _html(client, query)
    assert 'data-min="2026-10-09"' in html and 'data-max="2026-10-30"' in html
    assert f'data-start="{start}"' in html and f'data-end="{end}"' in html
