"""Seam: the Sessions page lists logical Sessions (QueryService.sessions, /sessions, /sessions.csv)."""
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


@pytest.fixture
def svc(root, service_for):
    _write(root)
    return service_for(root)


@pytest.fixture
def client(root, configure):
    _write(root)
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    return create_app(factory, lambda: NOW).test_client()


def test_one_row_per_session_with_columns_newest_activity_first(svc):
    rows = svc.sessions(ExploreSpec()).rows
    assert [r.session_id for r in rows] == ["s2bbbbbbbb-2222", "s1aaaaaaaa-1111"]
    b, a = rows
    assert (a.name, a.project, a.requests) == ("Login work", "proj-a", 2)
    assert a.cost == pytest.approx(40.0)
    assert a.share == pytest.approx(40 / 50)
    assert a.last_activity.isoformat().startswith("2026-10-29T11:00")
    assert b.name == "Cart" and b.requests == 1


def test_filters_narrow_rows_and_shares(svc):
    result = svc.sessions(ExploreSpec("date", {"project": ("proj-b",)}))
    assert [r.session_id for r in result.rows] == ["s2bbbbbbbb-2222"]
    assert result.rows[0].share == pytest.approx(1.0)


def test_page_lists_rows_linked_to_session_view_with_short_id(client):
    html = client.get("/sessions").get_data(as_text=True)
    assert "Login work" in html and "s1aaaaaa" in html
    assert 'href="/session/s1aaaaaaaa-1111"' in html
    for col in ("Project", "Last activity", "Cost", "Requests", "Share"):
        assert col in html
    assert "2026-10-29 11:00" in html


def test_whole_row_opens_the_session_view_and_the_name_stays_a_link(client):
    html = client.get("/sessions").get_data(as_text=True)
    assert 'data-href="/session/s1aaaaaaaa-1111"' in html
    assert 'href="/session/s1aaaaaaaa-1111"' in html


def test_nav_shows_sessions(client):
    assert ">Sessions</a>" in client.get("/").get_data(as_text=True)


def test_chart_and_csv_respect_filters(client):
    html = client.get("/sessions?project=proj-b").get_data(as_text=True)
    assert "Login work" not in html and "Cart" in html
    assert "/sessions.csv?" in html and "project=proj-b" in html
    assert 'id="cost-chart"' in html
    res = client.get("/sessions.csv?project=proj-b")
    assert res.mimetype == "text/csv"
    lines = res.get_data(as_text=True).strip().splitlines()
    assert lines[0] == "Session,Session id,Project,Last activity,Cost,Requests,Share %"
    assert lines[1:] == ["Cart,s2bbbbbbbb-2222,proj-b,2026-10-30 10:00,10.00,1,100.0"]


def test_csv_session_cost_includes_the_web_search_fee(root, service_for):
    from helpers import web_search_result_line, web_search_use_line
    ts = "2026-10-29T10:00:00Z"
    root.write_transcript("proj-a", "s1aaaaaaaa-1111", [req("m1", ts, output=M)], title="Login work",
                          extra_lines=[web_search_use_line(ts, "tu1", "m1"),
                                       web_search_result_line(ts, "tu1", 2)])
    csv_text = explore.sessions_to_csv(service_for(root).sessions(ExploreSpec("project", {})))
    assert csv_text.strip().splitlines()[1:] == [
        "Login work,s1aaaaaaaa-1111,proj-a,2026-10-29 10:00,20.02,1,100.0"]
