"""Seam: the Date filter's preset tokens (last-7 ... last-month): their resolution against a fixed
"today", and that they survive a reload through the URL, the chip and the filtered page."""
from datetime import date

import pytest

from claude_cost import cli, ingest, store
from claude_cost.explore import ExploreSpec, resolve_date
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req

FRIDAY = date(2026, 10, 30)


@pytest.mark.parametrize("token, today, expected", [
    ("last-7", FRIDAY, "2026-10-24..2026-10-30"),
    ("last-30", FRIDAY, "2026-10-01..2026-10-30"),
    ("last-90", FRIDAY, "2026-08-02..2026-10-30"),
    ("this-week", FRIDAY, "2026-10-26..2026-10-30"),  # Monday to today
    ("this-week", date(2026, 10, 26), "2026-10-26..2026-10-26"),
    ("last-week", FRIDAY, "2026-10-19..2026-10-25"),
    ("last-week", date(2026, 11, 1), "2026-10-19..2026-10-25"),  # a Sunday belongs to the week it ends
    ("this-month", FRIDAY, "2026-10-01..2026-10-30"),
    ("last-month", FRIDAY, "2026-09-01..2026-09-30"),
    ("last-month", date(2026, 1, 15), "2025-12-01..2025-12-31"),
    ("last-month", date(2026, 3, 31), "2026-02-01..2026-02-28"),
])
def test_presets_resolve_against_today(token, today, expected):
    assert resolve_date(token, today) == expected


def test_a_day_and_a_range_resolve_to_themselves():
    assert resolve_date("2026-10-05", FRIDAY) == "2026-10-05"
    assert resolve_date("2026-10-05..2026-10-07", FRIDAY) == "2026-10-05..2026-10-07"


def test_a_spec_resolves_only_its_date_values():
    spec = ExploreSpec("model", {"date": ("last-7", "2026-10-01"), "project": ("last-7",)})
    assert spec.resolved(FRIDAY).filters == {
        "date": ("2026-10-24..2026-10-30", "2026-10-01"), "project": ("last-7",)}
    assert spec.filters["date"][0] == "last-7"  # the original keeps the token for the URL


@pytest.fixture
def client(root, configure):
    root.write_transcript("proj-a", "s1aaaaaaaa-1111", [
        req("m1", "2026-10-29T10:00:00Z", output=1_000_000),
        req("m2", "2026-10-20T10:00:00Z", output=1_000_000),
        req("m3", "2026-09-10T10:00:00Z", output=1_000_000),
    ], title="Login work", cwd="/code/shop")
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    return create_app(factory, lambda: NOW).test_client()  # NOW is Friday 2026-10-30


def _days(client, token):
    html = client.get(f"/breakdowns?group=date&date={token}").get_data(as_text=True)
    return [d for d in ("2026-10-29", "2026-10-20", "2026-09-10") if f">{d}<" in html]


def test_pages_filter_by_the_preset_and_the_token_stays_in_the_url(client):
    assert _days(client, "this-week") == ["2026-10-29"]
    assert _days(client, "last-week") == ["2026-10-20"]
    assert _days(client, "last-month") == ["2026-09-10"]
    html = client.get("/sessions?date=last-week").get_data(as_text=True)
    assert "Login work" in html and "date=last-week" in html  # reloading the URL keeps the token


def test_the_chip_shows_the_preset_name_or_the_custom_range(client):
    preset = client.get("/sessions?date=last-30").get_data(as_text=True)
    assert "<strong>Last 30 days</strong>" in preset
    custom = client.get("/sessions?date=2026-10-01..2026-10-07").get_data(as_text=True)
    assert "<strong>2026-10-01 – 2026-10-07</strong>" in custom
    assert "<strong>2026-10-01</strong>" in client.get("/sessions?date=2026-10-01").get_data(as_text=True)


def test_removing_the_date_chip_means_all_time(client):
    html = client.get("/sessions?date=last-week").get_data(as_text=True)
    assert 'aria-label="Remove Date filter"' in html
    assert "date=" not in html.split('class="chip-x" href="')[1].split('"')[0]


def test_other_filters_options_see_the_resolved_preset(client):
    r = client.get("/filter-options?dimension=project&date=last-week")
    assert [o["count"] for o in r.get_json()["options"]] == [1]
    r = client.get("/filter-options?dimension=project&date=last-month")
    assert [o["count"] for o in r.get_json()["options"]] == [1]


def _dims(client):
    import json
    html = client.get("/sessions").get_data(as_text=True)
    blob = html.split('id="filter-dims" type="application/json">')[1].split("</script>")[0]
    return {d["key"]: d for d in json.loads(blob.replace("\\u0026", "&"))}


def test_the_date_view_lists_the_presets_and_bounds_the_calendar(client):
    date_dim = _dims(client)["date"]
    assert date_dim["kind"] == "date"
    assert [t for t, _ in date_dim["presets"]] == [
        "last-7", "last-30", "last-90", "this-week", "last-week", "this-month", "last-month"]
    assert date_dim["presets"][3] == ["this-week", "This week"]
    assert (date_dim["min"], date_dim["max"]) == ("2026-09-10", "2026-10-30")  # first request .. today
    assert date_dim["costsUrl"] == "/api/daily-cost"


def test_the_filter_script_and_the_dashboard_share_the_range_calendar(client):
    assert "range_calendar.js" in client.get("/sessions").get_data(as_text=True)
    assert "range_calendar.js" in client.get("/").get_data(as_text=True)
