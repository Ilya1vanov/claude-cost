"""Seam 2: Suggested sessions through the CLI and the web page (ticket #68), shape only."""
import io
import json
import os

import pytest

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req

M = 1_000_000


@pytest.fixture
def cenv(root, configure):
    root.write_transcript("p", "aaaa1111", [req("m1", "2026-10-29T01:00:00Z", output=M)], title="PROJ-1 work")
    root.write_transcript("p", "bbbb2222", [req("m2", "2026-10-29T02:00:00Z", output=M)], prompt="see PROJ-1 too")
    root.write_transcript("p", "cccc3333", [req("m3", "2026-10-29T03:00:00Z", output=M)], prompt="and PROJ-1 here")
    root.write_transcript("p", "dddd4444", [req("m4", "2026-10-29T04:00:00Z", output=M)], prompt="only PROJ-7")
    env = configure(root)
    with open(os.path.join(env["CLAUDE_COST_CONFIG_DIR"], "config.toml"), "a") as f:
        f.write('spotlight_key_pattern = "PROJ-\\\\d+"\n')
    return env


def ok(argv, env):
    out, err = io.StringIO(), io.StringIO()
    assert cli.main(["spotlight", *argv], env=env, now=NOW, out=out, err=err) == 0
    return out.getvalue()


def show(env, name):
    return json.loads(ok(["show", name, "--json"], env))


def test_cli_show_lists_suggested_and_add_accepts_remove_dismisses(cenv):
    data = show(cenv, "PROJ-1")
    assert [m["id"] for m in data["members"]] == ["aaaa1111"]
    assert [(s["id"], s["field"], s["snippet"]) for s in data["suggested"]] == [
        ("bbbb2222", "prompt", "see PROJ-1 too"), ("cccc3333", "prompt", "and PROJ-1 here")]
    assert set(data["suggested"][0]) == {"id", "label", "cost", "field", "snippet"}
    text = ok(["show", "PROJ-1"], cenv)
    assert "suggested bbbb2222" in text and "prompt: see PROJ-1 too" in text
    ok(["add", "PROJ-1", "bbbb2222"], cenv)
    ok(["remove", "PROJ-1", "cccc3333"], cenv)
    data = show(cenv, "PROJ-1")
    assert [m["id"] for m in data["members"]] == ["aaaa1111", "bbbb2222"] and data["suggested"] == []
    assert [d["id"] for d in data["dismissed"]] == ["cccc3333"]


def test_cli_add_for_a_key_without_an_auto_spotlight_creates_it(cenv):
    ok(["add", "PROJ-7", "dddd4444"], cenv)
    assert [m["id"] for m in show(cenv, "PROJ-7")["members"]] == ["dddd4444"]


def test_web_suggested_section_with_accept_and_dismiss(root, tmp_path):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=M)], title="PROJ-12 login")
    root.write_transcript("p", "s2", [req("m2", "2026-10-30T02:00:00Z", output=M)], prompt="mentions PROJ-12 only")
    root.write_transcript("p", "s3", [req("m3", "2026-10-30T03:00:00Z", output=M)], prompt="also PROJ-12")
    conn = store.connect(tmp_path / "web.sqlite")
    ingest.ingest(conn, [root.path])
    conn.close()
    client = create_app(lambda: QueryService(store.connect(tmp_path / "web.sqlite"), "UTC",
                                             key_pattern=r"PROJ-\d+"), lambda: NOW).test_client()
    page = client.get("/spotlights/PROJ-12").get_data(as_text=True)
    section = page.split('id="spotlight-suggested"')[1].split("</section>")[0]
    assert "mentions PROJ-12 only" in section and "prompt" in section
    assert "/spotlights/auto:PROJ-12/add" in section and "/spotlights/auto:PROJ-12/remove" in section
    assert client.post("/spotlights/PROJ-12/add", data={"session": "s2"}).status_code == 302
    assert client.post("/spotlights/PROJ-12/remove", data={"session": "s3"}).status_code == 302
    page = client.get("/spotlights/PROJ-12").get_data(as_text=True)
    assert 'id="spotlight-suggested"' not in page and "Dismissed sessions (1)" in page


def test_cli_suggestions_text_and_json(cenv):
    data = json.loads(ok(["suggestions", "--json"], cenv))
    assert [(g["key"], g["spotlight"]) for g in data] == [("PROJ-1", "auto:PROJ-1"), ("PROJ-7", None)]
    assert set(data[0]) == {"key", "spotlight", "name", "suggested"}
    assert set(data[0]["suggested"][0]) == {"id", "label", "cost", "field", "snippet"}
    assert [s["id"] for s in data[0]["suggested"]] == ["bbbb2222", "cccc3333"]
    text = ok(["suggestions"], cenv)
    assert "PROJ-1" in text and "bbbb2222" in text and "prompt: only PROJ-7" in text


def test_web_triage_session_page_and_spotlights_count(root, tmp_path):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=M)], title="PROJ-12 login")
    root.write_transcript("p", "s2", [req("m2", "2026-10-30T02:00:00Z", output=M)], prompt="mentions PROJ-12 only")
    root.write_transcript("p", "s3", [req("m3", "2026-10-30T03:00:00Z", output=M)], prompt="about PROJ-30")
    conn = store.connect(tmp_path / "web.sqlite")
    ingest.ingest(conn, [root.path])
    conn.close()
    client = create_app(lambda: QueryService(store.connect(tmp_path / "web.sqlite"), "UTC",
                                             key_pattern=r"PROJ-\d+"), lambda: NOW).test_client()
    page = client.get("/spotlights").get_data(as_text=True)
    assert 'id="pending-suggestions"' in page and "2 suggestions pending" in page and "/spotlights/triage" in page
    tri = client.get("/spotlights/triage").get_data(as_text=True)
    assert "PROJ-12" in tri and "PROJ-30" in tri and "about PROJ-30" in tri
    assert 'id="session-suggested"' in client.get("/session/s2").get_data(as_text=True)
    assert 'id="session-suggested"' not in client.get("/session/s1").get_data(as_text=True)
    assert client.post("/spotlights/triage/accept", data={"key": "PROJ-30", "session": "s3"}).status_code == 302
    assert client.post("/spotlights/triage/dismiss", data={"key": "PROJ-12", "session": "s2"}).status_code == 302
    page = client.get("/spotlights").get_data(as_text=True)
    assert 'id="pending-suggestions"' not in page
    assert "PROJ-30" in page  # accepting created the Auto-spotlight
