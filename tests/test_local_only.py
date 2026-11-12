"""Seam: the dashboard answers only requests addressed to, and posted from, the local machine
(a DNS-rebinding or cross-site page must not read prompts or change Spotlights)."""
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_cost import store
from claude_cost.query import QueryService
from claude_cost.web import create_app

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(tmp_path):
    conn = store.connect(tmp_path / "page.sqlite")
    return create_app(lambda: QueryService(conn, "UTC"), lambda: NOW).test_client()


@pytest.mark.parametrize("host", ["localhost", "localhost:8787", "127.0.0.1:8787", "[::1]:8787"])
def test_local_host_names_are_served(client, host):
    assert client.get("/healthz", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8787", "127.0.0.1.evil.example"])
def test_a_foreign_host_name_is_refused(client, host):
    assert client.get("/healthz", headers={"Host": host}).status_code == 400


def test_a_post_from_a_foreign_origin_is_refused_and_a_local_one_allowed(client):
    assert client.post("/refresh", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/refresh", headers={"Origin": "http://127.0.0.1:8787"}).status_code == 302
    assert client.post("/refresh").status_code == 302  # no Origin: not a browser cross-site post


def _hook():
    path = Path(__file__).parent.parent / "hooks" / "stale-cache-guard.py"
    spec = importlib.util.spec_from_file_location("stale_cache_guard", path)
    return path, spec


def test_hook_marker_is_not_written_through_a_planted_symlink(tmp_path):
    path, _ = _hook()
    ts = "2026-01-01T00:00:00Z"
    transcript = tmp_path / "t.jsonl"
    usage = {"input_tokens": 1, "cache_creation_input_tokens": 200000, "cache_read_input_tokens": 0}
    transcript.write_text(json.dumps({"type": "assistant", "timestamp": ts,
                                      "message": {"model": "opus", "usage": usage}}) + "\n")
    victim = tmp_path / "victim"
    victim.write_text("keep")
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    (tmpdir / f"stale-cache-ack-sid-{ts}").symlink_to(victim)
    run = subprocess.run([sys.executable, str(path)], capture_output=True, text=True,
                         input=json.dumps({"transcript_path": str(transcript), "session_id": "sid"}),
                         env={**os.environ, "TMPDIR": str(tmpdir)})
    assert victim.read_text() == "keep"
    assert run.returncode == 0  # the planted name counts as an existing marker: no write, no crash


def test_hook_session_id_cannot_escape_the_temp_directory(tmp_path):
    path, _ = _hook()
    ts = "2026-01-01T00:00:00Z"
    transcript = tmp_path / "t.jsonl"
    usage = {"input_tokens": 1, "cache_creation_input_tokens": 200000, "cache_read_input_tokens": 0}
    transcript.write_text(json.dumps({"type": "assistant", "timestamp": ts,
                                      "message": {"model": "opus", "usage": usage}}) + "\n")
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    run = subprocess.run([sys.executable, str(path)], capture_output=True, text=True,
                   input=json.dumps({"transcript_path": str(transcript), "session_id": "../escaped"}),
                   env={**os.environ, "TMPDIR": str(tmpdir)})
    assert run.returncode == 2 and list(tmpdir.glob("stale-cache-ack-*"))  # marker lands in the temp dir
    assert not (tmp_path / "escaped-2026-01-01T00:00:00Z").exists()
    assert not [p for p in tmp_path.rglob("*escaped*") if p.parent != tmpdir]


def test_hook_docstring_carries_no_real_usage_figures():
    path, _ = _hook()
    assert "$689" not in path.read_text()
