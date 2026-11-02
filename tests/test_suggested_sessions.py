"""Seam 1: Key match vs Suggested session (ticket #68) via the query service."""
from datetime import datetime

import pytest

from claude_cost.spotlights import SpotlightError
from helpers import req

M = 1_000_000  # $20 on opus-5-5
KEY = r"PROJ-\d+"


def write(root, sid, hour, **kw):
    root.write_transcript("p", sid, [req(f"m-{sid}", f"2026-10-29T{hour:02d}:00:00Z", output=M)], **kw)


def ids(detail):
    return {s.session_id for s in detail.sessions}


def suggested(detail):
    return {s.session_id: (s.field, s.snippet) for s in detail.suggested}


def test_classification_follows_the_title_first_rule(root, service_for):
    write(root, "t", 1, title="PROJ-1 work")                                   # title key
    write(root, "b", 2, branch="feature/PROJ-1-x")                             # branch key, no title key
    write(root, "w", 3, cwd="/repo/.claude/worktrees/proj-1")                  # worktree key, no title key
    write(root, "p", 4, prompt="please look at PROJ-1")                        # prompt only
    write(root, "o", 5, title="PROJ-2 other", branch="feature/PROJ-1-x")      # title names another key
    svc = service_for(root, key_pattern=KEY)
    d = svc.spotlight("PROJ-1")
    assert ids(d) == {"t", "b", "w"}
    assert d.summary.total == pytest.approx(60.0)
    assert suggested(d) == {"p": ("prompt", "please look at PROJ-1"),
                            "o": ("branch", "feature/PROJ-1-x")}
    assert ids(svc.spotlight("PROJ-2")) == {"o"}


def test_suggestions_are_not_counted_anywhere(root, service_for):
    write(root, "t", 1, title="PROJ-1 work")
    write(root, "p", 2, prompt="mentions PROJ-1")
    svc = service_for(root, key_pattern=KEY)
    assert [s.total for s in svc.spotlights()] == [pytest.approx(20.0)]
    ranked = svc.top_spotlights("last7", datetime(2026, 10, 30, 12))
    assert [r.summary.total for r in ranked] == [pytest.approx(20.0)]
    other = svc.create_spotlight("Other", ["t"])
    cmp = svc.spotlight_comparison(["PROJ-1", other])
    assert [c.summary.total for c in cmp] == [pytest.approx(20.0), pytest.approx(20.0)]


def test_a_key_found_only_in_suggestions_has_no_auto_spotlight(root, service_for):
    write(root, "p", 1, prompt="mentions PROJ-9")
    svc = service_for(root, key_pattern=KEY)
    assert svc.spotlights() == []
    with pytest.raises(KeyError):
        svc.spotlight("PROJ-9")


def test_adding_a_suggestion_for_a_key_without_an_auto_spotlight_creates_it(root, service_for):
    write(root, "p", 1, prompt="mentions PROJ-9")
    svc = service_for(root, key_pattern=KEY)
    svc.add_session("PROJ-9", "p")
    [s] = svc.spotlights()
    assert (s.kind, s.key, s.name) == ("auto", "PROJ-9", "PROJ-9")
    d = svc.spotlight("PROJ-9")
    assert ids(d) == {"p"} and d.picked == ("p",) and d.suggested == ()


def test_accept_picks_and_dismiss_dismisses_and_neither_reappears(root, service_for):
    write(root, "t", 1, title="PROJ-1 work")
    write(root, "a", 2, prompt="mentions PROJ-1")
    write(root, "d", 3, prompt="also PROJ-1")
    svc = service_for(root, key_pattern=KEY)
    svc.add_session("PROJ-1", "a")
    svc.remove_session("PROJ-1", "d")
    d = svc.spotlight("PROJ-1")
    assert ids(d) == {"t", "a"} and d.suggested == ()
    assert [x.session_id for x in d.dismissed] == ["d"]


def test_dismissing_a_suggestion_for_a_key_without_an_auto_spotlight(root, service_for):
    write(root, "p", 1, prompt="mentions PROJ-9")
    svc = service_for(root, key_pattern=KEY)
    svc.remove_session("PROJ-9", "p")
    svc.add_session("PROJ-9", "p")  # picking clears the dismissal and creates the Auto-spotlight
    assert ids(svc.spotlight("PROJ-9")) == {"p"}
    with pytest.raises(SpotlightError):
        svc.remove_session("PROJ-77", "p")  # nobody carries it


def test_suggested_session_stays_out_of_including_spotlights(root, service_for):
    write(root, "t", 1, title="PROJ-1 work")
    write(root, "p", 2, prompt="mentions PROJ-1")
    svc = service_for(root, key_pattern=KEY)
    svc.create_spotlight("Sprint", [], ["PROJ-1"])
    assert ids(svc.spotlight("Sprint")) == {"t"}
