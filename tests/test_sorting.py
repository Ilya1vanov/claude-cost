"""Seam: sorting and the 200-row cap on Sessions and Breakdowns (QueryService + /sessions, /breakdowns, CSVs)."""
from datetime import date, timedelta

import pytest

from claude_cost import cli, explore, ingest, store
from claude_cost.explore import ExploreSpec
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req

M = 1_000_000  # fixture model: output $20/M


def _write(root):
    root.write_transcript("proj-a", "s1aaaaaaaa-1111", [
        req("m1", "2026-10-29T10:00:00Z", output=M),
        req("m2", "2026-10-29T11:00:00Z", output=M),
    ], title="Login work")
    root.write_transcript("proj-b", "s2bbbbbbbb-2222", [
        req("m3", "2026-10-30T10:00:00Z", output=M // 2),
    ], title="Cart")


def _build(root, configure):
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    return factory


@pytest.fixture
def svc(root, service_for):
    _write(root)
    return service_for(root)


@pytest.fixture
def client(root, configure):
    _write(root)
    return create_app(_build(root, configure), lambda: NOW).test_client()


def _sessions(svc, sort=None):
    return [r.session_id[:2] for r in svc.sessions(ExploreSpec(sort=sort)).rows]


def _groups(svc, group, sort=None):
    return [r.value for r in svc.explore(ExploreSpec(group, sort=sort)).rows]


SESSION_SORTS = {
    "session": ["s2", "s1"],      # Cart < Login work
    "project": ["s1", "s2"],
    "last_activity": ["s1", "s2"],
    "cost": ["s2", "s1"],
    "requests": ["s2", "s1"],
    "share": ["s2", "s1"],
}


def test_sessions_default_is_last_activity_newest_first(svc):
    assert _sessions(svc) == ["s2", "s1"]
    assert svc.sessions(ExploreSpec()).sort == "-last_activity"


@pytest.mark.parametrize("key", SESSION_SORTS)
def test_every_session_sort_key_ascending_and_descending(svc, key):
    asc = SESSION_SORTS[key]
    assert _sessions(svc, key) == asc
    assert _sessions(svc, "-" + key) == asc[::-1]
    assert svc.sessions(ExploreSpec(sort="-" + key)).sort == "-" + key


def test_unknown_session_sort_key_is_rejected(svc):
    with pytest.raises(ValueError):
        svc.sessions(ExploreSpec(sort="bogus"))
    with pytest.raises(ValueError):
        svc.sessions(ExploreSpec(sort="-bogus"))


def test_breakdown_default_sorts(svc):
    assert _groups(svc, "date") == ["2026-10-30", "2026-10-29"]  # newest first
    assert svc.explore(ExploreSpec("date")).sort == "-value"
    assert _groups(svc, "project") == ["proj-a", "proj-b"]  # costliest first
    assert svc.explore(ExploreSpec("project")).sort == "-cost"


BREAKDOWN_SORTS = {
    "value": ["proj-a", "proj-b"],
    "cost": ["proj-b", "proj-a"],
    "requests": ["proj-b", "proj-a"],
    "share": ["proj-b", "proj-a"],
}


@pytest.mark.parametrize("key", BREAKDOWN_SORTS)
def test_every_breakdown_sort_key_ascending_and_descending(svc, key):
    asc = BREAKDOWN_SORTS[key]
    assert _groups(svc, "project", key) == asc
    assert _groups(svc, "project", "-" + key) == asc[::-1]


def test_unknown_breakdown_sort_key_is_rejected(svc):
    with pytest.raises(ValueError):
        svc.explore(ExploreSpec("project", sort="last_activity"))


def test_sort_round_trips_in_the_url_and_resets_on_drill_and_group_change():
    spec = ExploreSpec.from_query([("group", "model"), ("sort", "-requests")])
    assert spec.sort == "-requests"
    assert ("sort", "-requests") in spec.to_query()
    assert ("sort", "-requests") in spec.without("project", "x").to_query()
    assert spec.drill("project", "p").sort is None
    assert spec.with_group("date").sort is None
    assert all(k != "sort" for k, _ in ExploreSpec().to_query())


def test_pages_return_400_for_an_unknown_sort_key(client):
    assert client.get("/sessions?sort=bogus").status_code == 400
    assert client.get("/breakdowns?sort=bogus").status_code == 400
    assert client.get("/sessions.csv?sort=bogus").status_code == 400
    assert client.get("/breakdowns.csv?sort=bogus").status_code == 400
    assert client.get("/breakdowns?group=project&sort=last_activity").status_code == 400


def test_headers_link_to_sorts_and_the_active_one_shows_its_direction(client):
    html = client.get("/sessions").get_data(as_text=True)
    assert 'aria-sort="descending"' in html and html.count("aria-sort=") == 1
    assert "sort=last_activity" in html and "sort=cost" in html  # active flips; inactive sorts ascending
    html = client.get("/sessions?sort=cost").get_data(as_text=True)
    assert 'aria-sort="ascending"' in html and "sort=-cost" in html
    html = client.get("/breakdowns?group=project").get_data(as_text=True)
    assert 'aria-sort="descending"' in html and "sort=cost" in html


def test_sort_link_keeps_filters_and_group(client):
    html = client.get("/breakdowns?group=model&project=proj-a").get_data(as_text=True)
    links = [l for l in html.split('href="') if l.startswith("/breakdowns?") and "sort=" in l.split('"')[0]]
    assert links and all("group=model" in l and "project=proj-a" in l for l in links)


def test_csv_follows_the_sort(client):
    lines = client.get("/sessions.csv?sort=cost").get_data(as_text=True).splitlines()
    assert [l.split(",")[0] for l in lines[1:]] == ["Cart", "Login work"]
    lines = client.get("/breakdowns.csv?group=project&sort=value").get_data(as_text=True).splitlines()
    assert [l.split(",")[0] for l in lines[1:]] == ["proj-a", "proj-b"]


def test_cap_shows_at_most_200_rows_with_the_total(root, configure, service_for):
    first = date(2026, 1, 1)
    for i in range(explore.ROW_CAP + 1):
        d = first + timedelta(days=i)
        root.write_transcript("proj", f"sess{i:04d}-xxxx", [req(f"m{i}", f"{d}T10:00:00Z", output=1000)],
                              title=f"S{i}")
    svc = service_for(root)
    sessions = svc.sessions(ExploreSpec())
    assert len(sessions.rows) == 201 and len(sessions.shown) == 200
    assert sessions.shown[0] == sessions.rows[0]
    days = svc.explore(ExploreSpec("date"))
    assert len(days.rows) == 201 and len(days.shown) == 200

    client = create_app(_build(root, configure), lambda: NOW).test_client()
    for url in ("/sessions", "/breakdowns"):
        html = client.get(url).get_data(as_text=True)
        assert "Showing 200 of 201, narrow with filters." in html
    assert client.get("/sessions").get_data(as_text=True).count('<a href="/session/') == 200
    assert "Showing" not in client.get("/sessions?date=2026-01-01").get_data(as_text=True)
