"""Seam 1: Dismissed keys (ticket #71) via the query service."""
from datetime import datetime

import pytest

from claude_cost import ingest, store
from claude_cost.query import QueryService
from claude_cost.spotlights import SpotlightError
from helpers import req

M = 1_000_000  # $20 on opus-5-5
KEY = r"PROJ-\d+"


def write(root, sid, hour, **kw):
    root.write_transcript("p", sid, [req(f"m-{sid}", f"2026-10-29T{hour:02d}:00:00Z", output=M)], **kw)


def names(svc):
    return [s.name for s in svc.spotlights()]


def curated(root):
    write(root, "a1", 1, title="PROJ-1 work")
    write(root, "a2", 2, title="PROJ-1 more")
    write(root, "b1", 3, title="PROJ-2 other")
    write(root, "m", 4, prompt="mentions PROJ-1 only")


def test_a_dismissed_key_leaves_list_dashboard_compare_and_triage(root, service_for):
    curated(root)
    svc = service_for(root, key_pattern=KEY)
    svc.dismiss_key("PROJ-1")
    assert names(svc) == ["PROJ-2"]
    ranked = svc.top_spotlights("last7", datetime(2026, 10, 30, 12))
    assert [r.summary.name for r in ranked] == ["PROJ-2"]
    with pytest.raises(KeyError):
        svc.spotlight("PROJ-1")
    with pytest.raises(KeyError):
        svc.spotlight_comparison(["PROJ-1", "PROJ-2"])
    assert [g.key for g in svc.suggestions()] == []
    assert svc.session_suggestions("m") == []
    assert svc.dismissed_keys() == ["PROJ-1"]


def test_restore_key_brings_back_name_picks_and_dismissed_sessions(root, service_for):
    curated(root)
    svc = service_for(root, key_pattern=KEY)
    svc.rename_spotlight("PROJ-1", "Login")
    svc.add_sessions("PROJ-1", ["m"])
    svc.remove_session("PROJ-1", "a2")
    before = svc.spotlight("PROJ-1")
    svc.dismiss_key("PROJ-1")
    svc.restore_key("PROJ-1")
    after = svc.spotlight("PROJ-1")
    assert after.summary.name == "Login" and svc.dismissed_keys() == []
    assert {s.session_id for s in after.sessions} == {s.session_id for s in before.sessions} == {"a1", "m"}
    assert [d.session_id for d in after.dismissed] == ["a2"]


def test_an_including_spotlight_ignores_the_edge_while_the_key_is_dismissed(root, service_for):
    curated(root)
    svc = service_for(root, key_pattern=KEY)
    mine = svc.create_spotlight("Mine", ["b1"], includes=["PROJ-1"])
    assert svc.spotlight(mine).summary.sessions == 3
    svc.dismiss_key("PROJ-1")
    d = svc.spotlight(mine)
    assert d.summary.sessions == 1 and d.summary.includes == 0
    svc.restore_key("PROJ-1")
    assert svc.spotlight(mine).summary.sessions == 3


def test_a_dismissed_key_with_picks_only_is_also_hidden(root, service_for):
    write(root, "m", 4, prompt="mentions PROJ-9 only")
    svc = service_for(root, key_pattern=KEY)
    svc.add_sessions("PROJ-9", ["m"])
    assert names(svc) == ["PROJ-9"]
    svc.dismiss_key("PROJ-9")
    assert names(svc) == []


def test_dismiss_and_restore_validate(root, service_for):
    curated(root)
    svc = service_for(root, key_pattern=KEY)
    with pytest.raises(SpotlightError):
        svc.dismiss_key("PROJ-404")
    with pytest.raises(SpotlightError):
        svc.restore_key("PROJ-1")
    svc.dismiss_key("proj-1")  # key lookup ignores case, like detection
    svc.dismiss_key("PROJ-1")  # idempotent
    assert svc.dismissed_keys() == ["PROJ-1"]


def test_dismissed_keys_survive_rebuild(root, tmp_path):
    curated(root)
    conn = store.connect(tmp_path / "s.sqlite")
    ingest.ingest(conn, [root.path])
    QueryService(conn, "UTC", key_pattern=KEY).dismiss_key("PROJ-1")
    ingest.ingest(conn, [root.path], rebuild=True)
    assert QueryService(conn, "UTC", key_pattern=KEY).dismissed_keys() == ["PROJ-1"]


def test_restore_keeps_lock_state(root, service_for):
    curated(root)
    svc = service_for(root, key_pattern=KEY)
    svc.lock_spotlight("PROJ-1")
    svc.dismiss_key("PROJ-1")
    assert names(svc) == ["PROJ-2"]
    svc.restore_key("PROJ-1")
    assert {s.session_id for s in svc.spotlight("PROJ-1").sessions} == {"a1", "a2"}
    assert "m" in {x.session_id for x in svc.spotlight("PROJ-1").suggested}
