"""Included spotlights (ticket #56) via the query service: membership, nesting rules, deletion."""
import pytest

from claude_cost import ingest, store
from claude_cost.query import QueryService
from claude_cost.spotlights import SpotlightError
from helpers import req

M = 1_000_000  # $20 on opus-5-5
KEY = r"PROJ-\d+"


def sprint_root(root):
    """a1/a2: tickets PROJ-1 and PROJ-2 (one session each), shared and k: no key."""
    root.write_transcript("p", "a1", [req("m1", "2026-10-29T01:00:00Z", output=M)], branch="proj-1")
    root.write_transcript("p", "a2", [req("m2", "2026-10-29T02:00:00Z", output=M)], branch="proj-2")
    root.write_transcript("p", "shared", [req("m3", "2026-10-29T03:00:00Z", output=M)], prompt="shared")
    root.write_transcript("p", "k", [req("m4", "2026-10-29T04:00:00Z", output=M)], prompt="keyless")
    return root


@pytest.fixture
def svc(root, service_for):
    return service_for(sprint_root(root), key_pattern=KEY)


def ids(detail):
    return {s.session_id for s in detail.sessions}


def test_sprint_counts_the_members_of_included_spotlights_and_its_own_picks(svc):
    svc.create_spotlight("Sprint", ["k"], includes=["PROJ-1", "PROJ-2"])
    d = svc.spotlight("Sprint")
    assert ids(d) == {"a1", "a2", "k"}
    assert d.summary.total == pytest.approx(60.0) and d.summary.sessions == 3
    assert d.summary.includes == 2
    assert [(i.name, i.kind, i.sessions) for i in d.included] == [
        ("PROJ-1", "auto", 1), ("PROJ-2", "auto", 1)]
    assert d.included[0].cost == pytest.approx(20.0)
    by = {s.session_id: s for s in d.sessions}
    assert by["a1"].joined_as == "Included spotlight PROJ-1" and by["a1"].via == "PROJ-1"
    assert by["k"].joined_as == "Picked" and by["k"].via is None and by["k"].removable
    assert not by["a1"].removable


def test_a_session_shared_by_two_included_spotlights_counts_once(svc):
    svc.create_spotlight("X", ["shared"])
    svc.create_spotlight("Y", ["shared", "k"])
    svc.create_spotlight("Sprint", includes=["X", "Y"])
    d = svc.spotlight("Sprint")
    assert d.summary.total == pytest.approx(40.0) and d.summary.sessions == 2
    assert svc.spotlight("X").summary.total == pytest.approx(20.0)  # fully counted in both
    assert [s.also_in for s in d.sessions if s.session_id == "shared"] == [()]


def test_included_auto_spotlight_follows_its_key(root, tmp_path, service_for):
    svc = service_for(sprint_root(root), key_pattern=KEY)
    svc.create_spotlight("Sprint", includes=["PROJ-1"])
    root.write_transcript("p", "late", [req("m9", "2026-10-30T01:00:00Z", output=M)], branch="proj-1-more")
    conn = store.connect(tmp_path / "store.sqlite")
    ingest.ingest(conn, [root.path])
    assert ids(QueryService(conn, "UTC", key_pattern=KEY).spotlight("Sprint")) == {"a1", "late"}


def test_include_and_exclude_and_picked_sessions_still_work(svc):
    svc.create_spotlight("Sprint")
    svc.include_spotlight("Sprint", "PROJ-1")
    svc.add_session("Sprint", "k")
    assert ids(svc.spotlight("Sprint")) == {"a1", "k"}
    svc.exclude_spotlight("Sprint", "PROJ-1")
    assert ids(svc.spotlight("Sprint")) == {"k"} and svc.spotlight("Sprint").included == ()


@pytest.mark.parametrize("setup, includer, target", [
    ([], "Sprint", "Sprint"),                                        # itself
    ([("Big", ["Sprint"])], "Sprint", "Big"),                        # target includes others
    ([("Big", ["Other"])], "Sprint", "Big"),
    ([("Big", ["Sprint"])], "Sprint", "Other"),                      # includer is already included
    ([], "PROJ-1", "Sprint"),                                        # an Auto-spotlight can't include
    ([], "Sprint", "Nope"),                                          # unknown Spotlight
])
def test_nesting_rules_are_rejected_with_a_user_safe_error(svc, setup, includer, target):
    svc.create_spotlight("Sprint")
    svc.create_spotlight("Other")
    for name, inc in setup:
        svc.create_spotlight(name)
        for i in inc:
            svc.include_spotlight(name, i)
    with pytest.raises(SpotlightError):
        svc.include_spotlight(includer, target)


def test_creating_with_a_self_include_or_an_includer_target_is_rejected_and_leaves_nothing(svc):
    svc.create_spotlight("Big")
    svc.create_spotlight("Sprint", includes=["Big"])
    with pytest.raises(SpotlightError):
        svc.create_spotlight("New", ["k"], includes=["Sprint"])  # Sprint includes others
    with pytest.raises(SpotlightError):
        svc.create_spotlight("Loop", includes=["Loop"])
    for name in ("New", "Loop"):
        with pytest.raises(KeyError):
            svc.spotlight(name)


def test_deleting_an_included_spotlight_leaves_the_includer_valid(svc):
    svc.create_spotlight("X", ["shared"])
    svc.create_spotlight("Sprint", ["k"], includes=["X", "PROJ-1"])
    svc.delete_spotlight("X")
    d = svc.spotlight("Sprint")
    assert ids(d) == {"k", "a1"} and [i.name for i in d.included] == ["PROJ-1"]
    assert d.summary.includes == 1
    svc.delete_spotlight("Sprint")
    svc.create_spotlight("Sprint")
    assert svc.spotlight("Sprint").included == ()  # the edge to the old X is gone for good


def test_deleting_an_includer_frees_its_included_spotlights(svc):
    svc.create_spotlight("X")
    svc.create_spotlight("Sprint", includes=["X"])
    svc.delete_spotlight("Sprint")
    svc.create_spotlight("Y")
    svc.include_spotlight("X", "Y")  # X is no longer included, so it may include


def test_includable_spotlights_lists_only_the_allowed_ones(svc):
    svc.create_spotlight("X")
    svc.create_spotlight("Z")
    svc.create_spotlight("Big", includes=["Z"])
    svc.create_spotlight("Sprint", includes=["X"])
    assert svc.includable_spotlights("Sprint") == ["PROJ-1", "PROJ-2", "Z"]
    assert svc.includable_spotlights("PROJ-1") == []
    assert svc.includable_spotlights("Z") == []
