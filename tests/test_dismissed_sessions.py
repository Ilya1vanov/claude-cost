"""Seam 1: Dismissed sessions of an Auto-spotlight (ticket #67) via the query service."""
import pytest

from claude_cost.spotlights import SpotlightError
from helpers import req

M = 1_000_000  # $20 on opus-5-5
KEY = r"PROJ-\d+"


def world(root, desktop):
    """s1 carries PROJ-12; s2 is its Side session; s3 carries PROJ-12 and PROJ-13; s4 is unrelated."""
    root.write_transcript("p", "s1", [req("m1", "2026-10-29T01:00:00Z", output=M)], title="PROJ-12 login")
    root.write_transcript("p", "s2", [req("m2", "2026-10-29T02:00:00Z", output=M)], prompt="side chat")
    root.write_transcript("p", "s3", [req("m3", "2026-10-29T03:00:00Z", output=M)], title="PROJ-12 and PROJ-13")
    root.write_transcript("p", "s4", [req("m4", "2026-10-29T04:00:00Z", output=M)], prompt="other")
    desktop.write_session("d1", "s1")
    desktop.write_session("d2", "s2", spawnedFrom={"sessionId": "local_d1", "sideSession": True})


def ids(detail):
    return {s.session_id for s in detail.sessions}


def test_removing_a_key_match_dismisses_it_with_its_side_sessions(root, desktop, service_for):
    world(root, desktop)
    svc = service_for(root, desktop=desktop, key_pattern=KEY)
    assert ids(svc.spotlight("PROJ-12")) == {"s1", "s2", "s3"}
    assert {s.session_id for s in svc.spotlight("PROJ-12").sessions if s.removable} == {"s1", "s3"}
    svc.remove_session("PROJ-12", "s1")
    d = svc.spotlight("PROJ-12")
    assert ids(d) == {"s3"} and d.summary.total == pytest.approx(20.0)
    assert [x.session_id for x in d.dismissed] == ["s1"]


def test_a_side_session_that_qualifies_on_its_own_stays(root, desktop, service_for):
    world(root, desktop)
    svc = service_for(root, desktop=desktop, key_pattern=KEY)
    svc.add_session("PROJ-12", "s2")  # picked on its own
    svc.remove_session("PROJ-12", "s1")
    assert ids(svc.spotlight("PROJ-12")) == {"s2", "s3"}


def test_dismissal_is_per_auto_spotlight(root, desktop, service_for):
    world(root, desktop)
    svc = service_for(root, desktop=desktop, key_pattern=KEY)
    svc.remove_session("PROJ-12", "s3")
    assert "s3" not in ids(svc.spotlight("PROJ-12"))
    assert ids(svc.spotlight("PROJ-13")) == {"s3"}


def test_dismissal_propagates_into_including_spotlights_unless_picked_directly(root, desktop, service_for):
    world(root, desktop)
    svc = service_for(root, desktop=desktop, key_pattern=KEY)
    svc.create_spotlight("Sprint", ["s3"], ["PROJ-12"])
    assert ids(svc.spotlight("Sprint")) == {"s1", "s2", "s3"}
    svc.remove_session("PROJ-12", "s1")
    svc.remove_session("PROJ-12", "s3")
    assert ids(svc.spotlight("Sprint")) == {"s3"}  # s1 and its Side session left; s3 was picked directly


def test_restore_brings_it_back_and_picking_clears_the_dismissal(root, desktop, service_for):
    world(root, desktop)
    svc = service_for(root, desktop=desktop, key_pattern=KEY)
    svc.remove_session("PROJ-12", "s1")
    svc.restore_session("PROJ-12", "s1")
    d = svc.spotlight("PROJ-12")
    assert ids(d) == {"s1", "s2", "s3"} and d.dismissed == ()
    svc.remove_session("PROJ-12", "s1")
    svc.add_session("PROJ-12", "s1")
    d = svc.spotlight("PROJ-12")
    assert "s1" in ids(d) and d.dismissed == ()
    with pytest.raises(SpotlightError):
        svc.restore_session("Nope", "s1")


def test_removing_a_picked_key_carrier_dismisses_and_unpicks(root, desktop, service_for):
    world(root, desktop)
    svc = service_for(root, desktop=desktop, key_pattern=KEY)
    svc.add_session("PROJ-12", "s1")
    svc.remove_session("PROJ-12", "s1")
    d = svc.spotlight("PROJ-12")
    assert "s1" not in ids(d) and "s1" not in d.picked
    svc.restore_session("PROJ-12", "s1")
    d = svc.spotlight("PROJ-12")
    assert "s1" in ids(d) and "s1" not in d.picked


def test_dismissals_survive_rebuild(root, desktop, tmp_path):
    from claude_cost import ingest, store
    from claude_cost.query import QueryService
    world(root, desktop)
    conn = store.connect(tmp_path / "store.sqlite")
    stores = [desktop.path.parent.parent]
    ingest.ingest(conn, [root.path], desktop_stores=stores)
    QueryService(conn, "UTC", key_pattern=KEY).remove_session("PROJ-12", "s1")
    ingest.ingest(conn, [root.path], rebuild=True, desktop_stores=stores)
    assert ids(QueryService(conn, "UTC", key_pattern=KEY).spotlight("PROJ-12")) == {"s3"}
