"""Seam 1: Request ownership and Session grouping (ADR-0001), through the query service."""
import json
from datetime import date

import pytest

from helpers import req

D30 = date(2026, 10, 30)
M = 1_000_000  # output tokens = $20 on opus-5-5


def total(svc):
    d = svc.daily_spend(D30, D30)[0]
    return d.requests, d.cost


def by_id(svc):
    return {s.session_id: s for s in svc.session_summaries()}


@pytest.mark.parametrize("copied_session_id", [None, "orig"])
def test_fork_keeps_copied_history_with_the_original_session(root, service_for, copied_session_id):
    own = [req("m1", "2026-10-30T01:00:00Z", output=M), req("m2", "2026-10-30T02:00:00Z", output=M)]
    root.write_transcript("p", "orig", own)
    copied = [req(r.message_id, r.ts, output=M, forked_from="orig", session_id=copied_session_id)
              for r in own]
    root.write_transcript("p", "fork1", copied + [req("m3", "2026-10-30T03:00:00Z", output=M)])
    svc = service_for(root)
    s = by_id(svc)
    assert (s["orig"].requests, s["orig"].cost) == (2, pytest.approx(40.0))
    assert (s["fork1"].requests, s["fork1"].cost) == (1, pytest.approx(20.0))
    assert total(svc) == (3, pytest.approx(60.0))


def test_relocated_copy_adds_no_spend(root, service_for):
    r = [req("m1", "2026-10-30T01:00:00Z", output=M), req("m2", "2026-10-30T02:00:00Z", output=M)]
    root.write_transcript("proj-a", "s1", r)
    root.write_transcript("proj-b", "s1", r, extra_lines=[
        json.dumps({"type": "relocated", "sessionId": "s1", "relocatedCwd": "/new/place"})])
    svc = service_for(root)
    assert total(svc) == (2, pytest.approx(40.0))
    assert [(s.session_id, s.requests) for s in svc.session_summaries()] == [("s1", 2)]


def test_resume_copy_credits_requests_to_the_session_that_made_them(root, service_for):
    root.write_transcript("p", "orig", [req("m1", "2026-10-30T01:00:00Z", output=M)])
    root.write_transcript("p", "resumed", [
        req("m1", "2026-10-30T01:00:00Z", output=M, session_id="orig"),  # copied history
        req("m2", "2026-10-30T05:00:00Z", output=M),                      # made after resume
    ])
    svc = service_for(root)
    assert total(svc) == (2, pytest.approx(40.0))
    # one logical conversation, shown as one Session
    (s,) = svc.session_summaries()
    assert (s.session_id, s.requests, s.cost) == ("orig", 2, pytest.approx(40.0))
    assert s.transcripts == ("orig", "resumed")


def test_unmarked_copies_are_counted_once(root, service_for):
    r = [req("m1", "2026-10-30T01:00:00Z", output=M, session_id="gone"),
         req("m2", "2026-10-30T02:00:00Z", output=M, session_id="gone")]
    root.write_transcript("p", "a", r)
    root.write_transcript("q", "b", r)  # no marker, same uuids and message ids, no owner file
    svc = service_for(root)
    assert total(svc) == (2, pytest.approx(40.0))
    assert sum(s.requests for s in svc.session_summaries()) == 2


def test_continued_in_link_groups_transcripts_into_one_session(root, service_for):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=M)], extra_lines=[
        json.dumps({"type": "continued-in", "sessionId": "s1", "continuedInSessionId": "s2",
                    "timestamp": "2026-10-30T02:00:00Z"})])
    root.write_transcript("p", "s2", [req("m2", "2026-10-30T03:00:00Z", output=M)])
    root.write_transcript("p", "other", [req("m3", "2026-10-30T04:00:00Z", output=M)])
    svc = service_for(root)
    s = by_id(svc)
    assert set(s) == {"s1", "other"}
    assert (s["s1"].requests, s["s1"].transcripts) == (2, ("s1", "s2"))
    assert s["other"].requests == 1
