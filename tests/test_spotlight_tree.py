"""Session tree (ticket #57): parent ids within the Spotlight, tree order and the page render."""
import pytest

from claude_cost import ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req

M = 1_000_000  # $20 on opus-5-5


def tree_root(root, desktop):
    """orch -> w1 ($40) -> deep; orch -> w2; orch -> fk (fork); stray is a Side session of outsider."""
    for sid, n in [("orch", 1), ("w1", 2), ("w2", 1), ("deep", 1), ("fk", 1), ("stray", 1), ("out", 1)]:
        root.write_transcript("p", sid, [req("m-" + sid, f"2026-10-30T0{n}:00:00Z", output=n * M)],
                              prompt=sid)
    desktop.write_session("o", "orch")
    desktop.write_session("1", "w1", spawnedFrom={"sessionId": "local_o", "sideSession": True})
    desktop.write_session("2", "w2", spawnedFrom={"sessionId": "local_o", "sideSession": True})
    desktop.write_session("3", "deep", spawnedFrom={"sessionId": "local_1", "sideSession": True})
    desktop.write_session("4", "fk", forkedFromSessionId="local_o")
    desktop.write_session("x", "out")
    desktop.write_session("5", "stray", spawnedFrom={"sessionId": "local_x", "sideSession": True})
    return root


@pytest.fixture
def svc(root, desktop, service_for):
    return service_for(tree_root(root, desktop), desktop=desktop)


def rows(svc, name):
    return [(s.session_id, s.parent_id, s.depth) for s in svc.spotlight(name).sessions]


def test_side_sessions_nest_under_their_parent_with_subtree_totals(svc):
    svc.create_spotlight("T", ["orch", "fk"])
    d = svc.spotlight("T")
    assert rows(svc, "T") == [("orch", None, 0), ("w1", "orch", 1), ("deep", "w1", 2),
                              ("fk", "orch", 1), ("w2", "orch", 1)]
    by = {s.session_id: s for s in d.sessions}
    assert by["orch"].cost == pytest.approx(20)
    assert by["orch"].subtree_cost == pytest.approx(120)
    assert by["w1"].subtree_cost == pytest.approx(60) and by["deep"].subtree_cost == pytest.approx(20)
    assert by["fk"].joined_as == "Fork" and by["fk"].parent_id == "orch"


def test_a_member_whose_parent_is_outside_the_spotlight_is_a_root(svc):
    svc.create_spotlight("T", ["stray", "orch"])
    assert ("stray", None, 0) in rows(svc, "T")
    svc.create_spotlight("U", ["w1"])  # deep joins as w1's Side session; orch is not a member
    assert rows(svc, "U") == [("w1", None, 0), ("deep", "w1", 1)]


def test_page_renders_nested_rows_in_tree_order_with_subtree_total(root, desktop, tmp_path):
    tree_root(root, desktop)
    conn = store.connect(tmp_path / "web.sqlite")
    ingest.ingest(conn, [root.path], desktop_stores=[desktop.path.parent.parent])
    conn.close()

    def new():
        return QueryService(store.connect(tmp_path / "web.sqlite"), "UTC")
    s = new()
    s.create_spotlight("T", ["orch"])
    s.close()
    html = create_app(new, lambda: NOW).test_client().get("/spotlights/T").get_data(as_text=True)
    table = html.split('id="spotlight-sessions"')[1].split("</table>")[0]
    pos = [table.index(sid) for sid in ("orch", "w1", "deep", "w2")]
    assert pos == sorted(pos)
    assert 'data-depth="2"' in table and 'data-depth="0"' in table
    assert "$100.00" in table  # orch subtree: 20 + 40 + 20 + 20
