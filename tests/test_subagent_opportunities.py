"""Seam 1: Wide subagent and Fork subagent Opportunities through the query service."""
import pytest

from claude_cost import ingest, store
from claude_cost.query import QueryService

from helpers import req

# claude-opus-5-5: input $5/M? use cost relations rather than literals where possible


def _by_id(findings):
    return {f.id: f for f in findings}


def _sub(root, agent, reqs, fork=False, sid="s1"):
    meta = {"agentType": "fork", "isFork": True} if fork else {"agentType": "general-purpose"}
    root.write_subagent_transcript("p", sid, agent, reqs, meta=meta)


def _setup(root):
    root.write_transcript("p", "s1", [req("main", "2026-10-30T10:00:00Z", input=1_000)])
    _sub(root, "wide1", [req("w-a", "2026-10-30T10:01:00Z", input=60_000),
                         req("w-b", "2026-10-30T10:02:00Z", input=1_000)])
    _sub(root, "small1", [req("s-a", "2026-10-30T10:03:00Z", input=10_000)])
    _sub(root, "fork1", [req("f-a", "2026-10-30T10:04:00Z", input=2_000)], fork=True)


def test_wide_subagent_covers_subagents_with_large_first_request(root, service_for):
    _setup(root)
    f = _by_id(service_for(root).findings())["wide-subagent"]
    assert f.kind == "opportunity"
    assert f.formula is None and f.avoidable is None
    assert {r.message_id for r in f.requests} == {"w-a", "w-b"}
    assert f.attributed == pytest.approx(sum(r.cost for r in f.requests))
    assert [(s.agent_id, s.requests) for s in f.subagents] == [("wide1", 2)]
    assert f.subagents[0].session_id == "s1"


def test_wide_threshold_is_configurable(root, tmp_path):
    _setup(root)
    conn = store.connect(tmp_path / "t.sqlite")
    ingest.ingest(conn, [root.path])
    svc = QueryService(conn, "UTC", thresholds={"wide_subagent_context_tokens": 5_000})
    f = _by_id(svc.findings())["wide-subagent"]
    assert {s.agent_id for s in f.subagents} == {"wide1", "small1"}


def test_fork_subagent_uses_fork_flag(root, service_for):
    _setup(root)
    f = _by_id(service_for(root).findings())["fork-subagent"]
    assert f.kind == "opportunity"
    assert f.formula is None and f.avoidable is None
    assert [r.message_id for r in f.requests] == ["f-a"]
    assert [s.agent_id for s in f.subagents] == ["fork1"]
    assert all(r.avoidable is None for r in f.requests)
