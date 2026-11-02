"""Spotlight ids and rename (ticket #66, ADR-0004): query service, migration, CLI and web."""
import io
import json
import os

import pytest

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.spotlights import SpotlightError
from claude_cost.web import create_app
from conftest import NOW
from helpers import req

M = 1_000_000  # $20 on opus-5-5
KEY = r"PROJ-\d+"


def ticket_root(root):
    """a1/a2: tickets PROJ-1 and PROJ-2; k: keyless."""
    root.write_transcript("p", "a1", [req("m1", "2026-10-29T01:00:00Z", output=M)], branch="proj-1")
    root.write_transcript("p", "a2", [req("m2", "2026-10-29T02:00:00Z", output=M)], branch="proj-2")
    root.write_transcript("p", "k", [req("m3", "2026-10-29T03:00:00Z", output=M)], prompt="keyless")
    return root


@pytest.fixture
def svc(root, service_for):
    return service_for(ticket_root(root), key_pattern=KEY)


def by_name(svc):
    return {s.name: s for s in svc.spotlights()}


def ids(detail):
    return {s.session_id for s in detail.sessions}


def test_user_spotlights_get_generated_ids_and_auto_ids_come_from_the_key(svc):
    svc.create_spotlight("Sprint", ["k"])
    svc.create_spotlight("Other")
    rows = by_name(svc)
    assert rows["Sprint"].id != rows["Other"].id and rows["Sprint"].id != "Sprint"
    assert rows["Sprint"].key == ""
    assert rows["PROJ-1"].id == "auto:PROJ-1" and rows["PROJ-1"].key == "PROJ-1"


def test_renaming_a_user_spotlight_keeps_picks_includes_and_includers(svc):
    svc.create_spotlight("Sprint", ["k"], includes=["PROJ-1"])
    svc.create_spotlight("Release", ["a2"])
    svc.create_spotlight("Wrapper", includes=["Release"])
    svc.rename_spotlight("Sprint", "Sprint 5")
    d = svc.spotlight("Sprint 5")
    assert ids(d) == {"a1", "k"} and d.picked == ("k",) and [i.name for i in d.included] == ["PROJ-1"]
    svc.rename_spotlight("Release", "Ship it")
    w = svc.spotlight("Wrapper")
    assert ids(w) == {"a2"} and [i.name for i in w.included] == ["Ship it"]
    with pytest.raises(KeyError):
        svc.spotlight("Sprint")


def test_renaming_an_auto_spotlight_keeps_key_matches_picks_and_edges_and_the_key(root, svc):
    svc.add_session("PROJ-1", "k")
    svc.create_spotlight("Sprint", includes=["PROJ-1"])
    svc.rename_spotlight("PROJ-1", "Login rework")
    d = svc.spotlight("Login rework")
    assert d.summary.key == "PROJ-1" and d.summary.name == "Login rework" and d.summary.kind == "auto"
    assert ids(d) == {"a1", "k"} and d.picked == ("k",)
    assert ids(svc.spotlight("Sprint")) == {"a1", "k"}
    # still collects sessions by key after the rename
    root.write_transcript("p", "a3", [req("m4", "2026-10-29T04:00:00Z", output=M)], branch="proj-1")
    ingest.ingest(svc._conn, [root.path])
    assert ids(QueryService(svc._conn, "UTC", key_pattern=KEY).spotlight("auto:PROJ-1")) == {"a1", "a3", "k"}


def test_empty_names_are_refused_and_duplicates_allowed(svc):
    svc.create_spotlight("Sprint")
    svc.create_spotlight("Other")
    with pytest.raises(SpotlightError):
        svc.rename_spotlight("Sprint", "   ")
    with pytest.raises(SpotlightError):
        svc.rename_spotlight("PROJ-1", "")
    svc.rename_spotlight("Other", "Sprint")
    svc.rename_spotlight("PROJ-2", "Sprint")
    assert [s.name for s in svc.spotlights()].count("Sprint") == 3


def test_references_resolve_by_id_then_key_then_unique_name(svc):
    svc.create_spotlight("Sprint", ["k"])
    sprint = by_name(svc)["Sprint"].id
    assert svc.resolve_spotlight(sprint) == sprint
    assert svc.resolve_spotlight("Sprint") == sprint
    assert svc.resolve_spotlight("PROJ-1") == "auto:PROJ-1"
    svc.rename_spotlight("PROJ-1", "Sprint")
    assert svc.resolve_spotlight("PROJ-1") == "auto:PROJ-1"  # key still wins over names
    with pytest.raises(SpotlightError) as e:
        svc.resolve_spotlight("Sprint")
    assert sprint in str(e.value) and "auto:PROJ-1" in str(e.value)
    with pytest.raises(SpotlightError):
        svc.add_sessions("Sprint", ["a2"])
    svc.add_sessions(sprint, ["a2"])
    assert ids(svc.spotlight(sprint)) == {"k", "a2"}
    with pytest.raises(SpotlightError, match="Nope"):
        svc.resolve_spotlight("Nope")


def _name_keyed_store(root, tmp_path):
    """A store as the name-keyed code left it, then reopened (which migrates)."""
    conn = store.connect(tmp_path / "store.sqlite")
    ingest.ingest(conn, [root.path])
    with conn:
        conn.executescript("""
            DROP TABLE spotlights;
            CREATE TABLE spotlights (name TEXT PRIMARY KEY, rule TEXT NOT NULL DEFAULT '');
            DELETE FROM spotlight_pins; DELETE FROM spotlight_includes;
            INSERT INTO spotlights (name) VALUES ('Sprint'), ('Wrapper'), ('PROJ-2');
            INSERT INTO spotlight_pins VALUES ('Sprint', 'k'), ('PROJ-1', 'k'), ('PROJ-2', 'a1');
            INSERT INTO spotlight_includes VALUES ('Wrapper', 'Sprint'), ('Wrapper', 'PROJ-1');
        """)
    conn.close()
    return store.connect(tmp_path / "store.sqlite")


def test_migration_moves_names_to_ids_keeping_membership_and_totals(root, tmp_path):
    ticket_root(root)
    conn = _name_keyed_store(root, tmp_path)
    svc = QueryService(conn, "UTC", key_pattern=KEY)
    rows = by_name(svc)
    assert ids(svc.spotlight("Sprint")) == {"k"}
    assert ids(svc.spotlight("Wrapper")) == {"k", "a1"}
    assert svc.spotlight("Wrapper").summary.total == pytest.approx(40.0)
    assert ids(svc.spotlight("PROJ-1")) == {"a1", "k"}   # Auto-spotlight pick moved to its id
    # a user-defined Spotlight named like a key keeps today's behaviour: no Auto beside it
    assert rows["PROJ-2"].kind == "picked" and ids(svc.spotlight("PROJ-2")) == {"a1"}
    assert rows["Sprint"].id != "Sprint" and rows["Wrapper"].includes == 2
    ingest.ingest(conn, [root.path], rebuild=True)       # survives a rebuild
    again = QueryService(store.connect(tmp_path / "store.sqlite"), "UTC", key_pattern=KEY)
    assert ids(again.spotlight("Wrapper")) == {"k", "a1"}
    assert again.spotlight("Wrapper").summary.total == pytest.approx(40.0)


def test_migration_runs_once(root, tmp_path):
    ticket_root(root)
    conn = _name_keyed_store(root, tmp_path)
    before = sorted(tuple(r) for r in conn.execute("SELECT id, name FROM spotlights"))
    conn.close()
    after = store.connect(tmp_path / "store.sqlite")
    assert sorted(tuple(r) for r in after.execute("SELECT id, name FROM spotlights")) == before


# ---- adapters: shape only ---------------------------------------------------------------

@pytest.fixture
def cenv(root, configure):
    ticket_root(root)
    env = configure(root)
    with open(os.path.join(env["CLAUDE_COST_CONFIG_DIR"], "config.toml"), "a") as f:
        f.write('spotlight_key_pattern = "PROJ-\\\\d+"\n')
    return env


def run(argv, env):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["spotlight", *argv], env=env, now=NOW, out=out, err=err)
    return code, out.getvalue(), [l for l in err.getvalue().splitlines() if not l.startswith("warning:")]


def ok(argv, env):
    code, out, err = run(argv, env)
    assert (code, err) == (0, [])
    return out


def test_cli_list_and_show_print_ids_and_rename_works_by_id_key_or_name(cenv):
    ok(["create", "Sprint", "--session", "k"], cenv)
    rows = {r["name"]: r for r in json.loads(ok(["list", "--json"], cenv))}
    sid = rows["Sprint"]["id"]
    assert rows["PROJ-1"]["id"] == "auto:PROJ-1" and rows["PROJ-1"]["key"] == "PROJ-1"
    assert rows["Sprint"]["key"] == ""
    assert sid in ok(["list"], cenv) and sid in ok(["show", "Sprint"], cenv)
    assert json.loads(ok(["show", sid, "--json"], cenv))["id"] == sid
    ok(["rename", "PROJ-1", "Login rework"], cenv)
    ok(["rename", sid, "Sprint 5"], cenv)
    shown = json.loads(ok(["show", "Login rework", "--json"], cenv))
    assert shown["name"] == "Login rework" and shown["key"] == "PROJ-1" and shown["id"] == "auto:PROJ-1"
    assert "PROJ-1" in ok(["show", "Login rework"], cenv)
    assert json.loads(ok(["show", "Sprint 5", "--json"], cenv))["id"] == sid


def test_cli_ambiguous_name_fails_listing_ids_and_empty_name_is_refused(cenv):
    ok(["create", "Sprint"], cenv)
    ok(["rename", "PROJ-1", "Sprint"], cenv)
    code, out, err = run(["show", "Sprint"], cenv)
    assert code != 0 and out == "" and len(err) == 1 and "auto:PROJ-1" in err[0]
    code, out, err = run(["rename", "PROJ-2", " "], cenv)
    assert code != 0 and len(err) == 1


def _client(root, tmp_path):
    conn = store.connect(tmp_path / "web.sqlite")
    ingest.ingest(conn, [root.path])
    conn.close()

    def service():
        return QueryService(store.connect(tmp_path / "web.sqlite"), "UTC", key_pattern=KEY)
    return create_app(service, lambda: NOW).test_client(), service


def test_web_urls_use_ids_and_the_page_renames(root, tmp_path):
    ticket_root(root)
    client, new = _client(root, tmp_path)
    svc = new()
    sid = svc.create_spotlight("Sprint", ["k"])
    svc.close()
    listing = client.get("/spotlights").get_data(as_text=True)
    assert f"/spotlights/{sid}" in listing and "/spotlights/auto:PROJ-1" in listing
    page = client.get(f"/spotlights/{sid}").get_data(as_text=True)
    assert 'action="/spotlights/%s/rename"' % sid in page
    r = client.post(f"/spotlights/{sid}/rename", data={"name": "Sprint 5"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/spotlights/{sid}")
    assert "Sprint 5" in client.get(f"/spotlights/{sid}").get_data(as_text=True)
    assert client.post(f"/spotlights/{sid}/rename", data={"name": ""}).status_code == 400
    client.post("/spotlights/auto:PROJ-1/rename", data={"name": "Login rework"})
    auto = client.get("/spotlights/auto:PROJ-1").get_data(as_text=True)
    assert "Login rework" in auto and "PROJ-1" in auto
    assert client.get("/spotlights/Login rework").status_code == 200  # unique name still resolves
