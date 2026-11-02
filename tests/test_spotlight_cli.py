"""`claude-cost spotlight …` (ticket #58): happy paths, JSON shapes and one-line errors."""
import io
import json
import os
import subprocess
import sys

import pytest

from claude_cost import cli
from conftest import NOW
from helpers import req

M = 1_000_000  # $20 on opus-5-5


@pytest.fixture
def cenv(root, configure):
    root.write_transcript("p", "aaaa1111", [req("m1", "2026-10-29T01:00:00Z", output=M)],
                          branch="proj-1", title="Ticket one")
    root.write_transcript("p", "bbbb2222", [req("m2", "2026-10-29T02:00:00Z", output=M)],
                          branch="proj-2", title="Ticket two")
    root.write_transcript("p", "cccc3333", [req("m3", "2026-10-29T03:00:00Z", output=M)],
                          prompt="keyless work", title="Keyless topic")
    env = configure(root)
    with open(os.path.join(env["CLAUDE_COST_CONFIG_DIR"], "config.toml"), "a") as f:
        f.write('spotlight_key_pattern = "PROJ-\\\\d+"\n')
    return env


def run(argv, env):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["spotlight", *argv], env=env, now=NOW, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def errors(err):
    """stderr without the first-run retention warning."""
    return [l for l in err.splitlines() if not l.startswith("warning:")]


def ok(argv, env):
    code, out, err = run(argv, env)
    assert (code, errors(err)) == (0, [])
    return out


def fails(argv, env, *words):
    code, out, err = run(argv, env)
    assert code != 0 and out == ""
    (line,) = errors(err)
    assert line.startswith("claude-cost: ")
    for w in words:
        assert w in line


def show(env, name):
    return json.loads(ok(["show", name, "--json"], env))


def names(env):
    return {r["name"] for r in json.loads(ok(["list", "--json"], env))}


def test_create_show_and_list(cenv):
    ok(["create", "Sprint 5", "--session", "cccc3333", "--include", "PROJ-1", "--include", "PROJ-2"], cenv)
    data = show(cenv, "Sprint 5")
    assert data["name"] == "Sprint 5" and data["kind"] == "picked"
    assert data["total"] == pytest.approx(60.0) and data["sessions"] == 3 and data["includes"] == 2
    assert data["picked"] == ["cccc3333"]
    assert [(i["name"], i["kind"], i["sessions"]) for i in data["included"]] == [
        ("PROJ-1", "auto", 1), ("PROJ-2", "auto", 1)]
    by = {s["id"]: s for s in data["members"]}
    assert set(by) == {"aaaa1111", "bbbb2222", "cccc3333"}
    assert by["aaaa1111"]["label"] == "Ticket one" and by["aaaa1111"]["cost"] == pytest.approx(20.0)
    assert by["aaaa1111"]["via"] == "PROJ-1" and by["cccc3333"]["picked"] is True
    assert {"id", "label", "cost", "requests", "joined_as", "picked", "key_matched", "via"} == set(by["cccc3333"])

    rows = {r["name"]: r for r in json.loads(ok(["list", "--json"], cenv))}
    assert set(rows) == {"Sprint 5", "PROJ-1", "PROJ-2"}
    assert {"id", "key", "name", "kind", "locked", "total", "requests", "sessions", "includes",
            "first_activity", "last_activity"} == set(rows["Sprint 5"])
    assert rows["Sprint 5"]["includes"] == 2 and rows["PROJ-1"]["kind"] == "auto"

    text = ok(["list"], cenv)
    assert "Sprint 5" in text and "$60.00" in text and "3 sessions" in text
    shown = ok(["show", "Sprint 5"], cenv)
    assert "Sprint 5" in shown and "Ticket one" in shown and "PROJ-2" in shown


def test_add_remove_include_exclude_delete(cenv):
    ok(["create", "S"], cenv)
    ok(["add", "S", "aaaa1111", "cccc3333"], cenv)
    assert show(cenv, "S")["picked"] == ["aaaa1111", "cccc3333"]
    ok(["remove", "S", "aaaa1111"], cenv)
    assert show(cenv, "S")["picked"] == ["cccc3333"]
    ok(["include", "S", "PROJ-1", "PROJ-2"], cenv)
    assert show(cenv, "S")["includes"] == 2
    ok(["exclude", "S", "PROJ-1"], cenv)
    assert [i["name"] for i in show(cenv, "S")["included"]] == ["PROJ-2"]
    ok(["delete", "S"], cenv)
    assert "S" not in names(cenv)


def test_add_to_an_auto_spotlight(cenv):
    ok(["add", "PROJ-1", "cccc3333"], cenv)
    assert show(cenv, "PROJ-1")["sessions"] == 2


def test_search_uses_the_picker_ranking(cenv):
    found = json.loads(ok(["search", "ticket", "--json"], cenv))
    assert {r["id"] for r in found} == {"aaaa1111", "bbbb2222"}
    assert set(found[0]) == {"id", "label", "cost", "kind"}
    svc = cli._open(cli.load_config(cenv))
    assert [r["id"] for r in found] == [c.session_id for c in svc.search_sessions("ticket", "")]
    assert "aaaa1111" in ok(["search", "ticket"], cenv)
    assert json.loads(ok(["search", "zzzzzz", "--json"], cenv)) == []


def test_unknown_spotlights_are_one_line_errors(cenv):
    ok(["create", "S"], cenv)
    fails(["show", "Nope"], cenv, "Nope")
    fails(["delete", "Nope"], cenv, "Nope")
    fails(["add", "Nope", "aaaa1111"], cenv, "Nope")
    fails(["remove", "Nope", "aaaa1111"], cenv, "Nope")
    fails(["include", "Nope", "PROJ-1"], cenv, "Nope")
    fails(["include", "S", "Nope"], cenv, "Nope")
    fails(["exclude", "Nope", "PROJ-1"], cenv, "Nope")
    fails(["create", "T", "--include", "Nope"], cenv, "Nope")
    assert "T" not in names(cenv)


def test_unknown_sessions_are_one_line_errors(cenv):
    ok(["create", "S"], cenv)
    fails(["add", "S", "aaaa1111", "ghost"], cenv, "ghost")
    assert show(cenv, "S")["picked"] == []  # nothing saved
    fails(["create", "T", "--session", "ghost"], cenv, "ghost")
    assert "T" not in names(cenv)


def test_invalid_nesting_and_duplicates_are_one_line_errors(cenv):
    ok(["create", "Sprint", "--include", "PROJ-1"], cenv)
    ok(["create", "Other"], cenv)
    fails(["include", "Other", "Sprint"], cenv, "cannot be included")
    fails(["include", "Sprint", "Sprint"], cenv, "itself")
    fails(["include", "PROJ-2", "PROJ-1"], cenv, "Auto-spotlight")
    ok(["create", "Sprint"], cenv)  # names need not be unique


def test_runs_as_a_subprocess(cenv):
    env = {**os.environ, **cenv}

    def sub(*argv):
        return subprocess.run([sys.executable, "-c", "from claude_cost.cli import main_entry; main_entry()",
                               "spotlight", *argv], env=env, capture_output=True, text=True)

    assert sub("create", "S", "--session", "aaaa1111").returncode == 0
    r = sub("show", "S", "--json")
    assert r.returncode == 0 and json.loads(r.stdout)["picked"] == ["aaaa1111"]
    r = sub("show", "Nope")
    assert r.returncode != 0 and r.stdout == "" and len(errors(r.stderr)) == 1


def test_remove_dismisses_a_key_match_and_restore_brings_it_back(cenv):
    ok(["remove", "PROJ-1", "aaaa1111"], cenv)
    data = show(cenv, "PROJ-1")
    assert data["members"] == [] and data["sessions"] == 0
    assert [(d["id"], d["label"]) for d in data["dismissed"]] == [("aaaa1111", "Ticket one")]
    assert set(data["dismissed"][0]) == {"id", "label", "cost"}
    text = ok(["show", "PROJ-1"], cenv)
    assert "dismissed" in text and "aaaa1111" in text
    ok(["restore", "PROJ-1", "aaaa1111"], cenv)
    data = show(cenv, "PROJ-1")
    assert [m["id"] for m in data["members"]] == ["aaaa1111"] and data["dismissed"] == []
    fails(["restore", "PROJ-1", "aaaa1111"], cenv, "not dismissed")
    fails(["restore", "Nope", "aaaa1111"], cenv, "Nope")
