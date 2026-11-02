"""Seam 1: Locked auto-spotlights (ticket #69) via the query service, plus adapter smoke tests."""
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


def write(root, sid, hour, **kw):
    root.write_transcript("p", sid, [req(f"m-{sid}", f"2026-10-29T{hour:02d}:00:00Z", output=M)], **kw)


def ids(detail):
    return {s.session_id for s in detail.sessions}


def sug(detail):
    return {s.session_id for s in detail.suggested}


def base(root):
    write(root, "t1", 1, title="PROJ-1 one")
    write(root, "t2", 2, title="PROJ-1 two")
    write(root, "p", 3, prompt="mentions PROJ-1")      # pending suggestion
    return root


def test_lock_keeps_total_and_members_and_new_carriers_are_only_suggested(root, service_for):
    svc = service_for(base(root), key_pattern=KEY)
    assert svc.spotlight("PROJ-1").summary.locked is False
    svc.lock_spotlight("PROJ-1")
    d = svc.spotlight("PROJ-1")
    assert d.summary.locked is True
    assert ids(d) == {"t1", "t2"} and d.summary.total == pytest.approx(40.0)
    assert set(d.picked) == {"t1", "t2"}
    assert sug(d) == {"p"}                              # pending suggestions stay suggestions
    write(root, "n", 4, title="PROJ-1 new")             # a new carrier, even with the key in its title
    svc = service_for(root, key_pattern=KEY)
    d = svc.spotlight("PROJ-1")
    assert ids(d) == {"t1", "t2"} and d.summary.total == pytest.approx(40.0)
    assert sug(d) == {"p", "n"}
    assert [s.total for s in svc.spotlights()] == [pytest.approx(40.0)]


def test_accepting_a_suggestion_of_a_locked_one_makes_it_a_member(root, service_for):
    svc = service_for(base(root), key_pattern=KEY)
    svc.lock_spotlight("PROJ-1")
    svc.add_session("PROJ-1", "p")
    d = svc.spotlight("PROJ-1")
    assert ids(d) == {"t1", "t2", "p"} and d.suggested == ()


def test_removing_a_key_carrier_from_a_locked_one_dismisses_it(root, service_for):
    svc = service_for(base(root), key_pattern=KEY)
    svc.lock_spotlight("PROJ-1")
    svc.remove_session("PROJ-1", "t2")
    d = svc.spotlight("PROJ-1")
    assert ids(d) == {"t1"} and "t2" not in sug(d)
    assert [x.session_id for x in d.dismissed] == ["t2"]


def test_unlock_lets_key_matches_join_again_and_keeps_dismissals(root, service_for):
    svc = service_for(base(root), key_pattern=KEY)
    svc.lock_spotlight("PROJ-1")
    svc.remove_session("PROJ-1", "t2")
    write(root, "n", 4, title="PROJ-1 new")
    svc = service_for(root, key_pattern=KEY)
    svc.unlock_spotlight("PROJ-1")
    d = svc.spotlight("PROJ-1")
    assert d.summary.locked is False
    assert ids(d) == {"t1", "n"} and [x.session_id for x in d.dismissed] == ["t2"]
    assert sug(d) == {"p"}


def test_lock_state_survives_a_rebuild_and_a_locked_one_outlives_its_picks(root, service_for, tmp_path):
    svc = service_for(base(root), key_pattern=KEY)
    svc.lock_spotlight("PROJ-1")
    svc.rename_spotlight("PROJ-1", "Login")
    ingest.ingest(svc._conn, [root.path], rebuild=True)
    again = QueryService(store.connect(tmp_path / "store.sqlite"), "UTC", key_pattern=KEY)
    d = again.spotlight("Login")
    assert d.summary.locked and d.summary.key == "PROJ-1" and ids(d) == {"t1", "t2"}
    again.remove_session("PROJ-1", "t1")
    again.remove_session("PROJ-1", "t2")
    assert again.spotlight("PROJ-1").summary.locked   # still there, empty


def test_lock_and_unlock_need_an_auto_spotlight(root, service_for):
    svc = service_for(base(root), key_pattern=KEY)
    svc.create_spotlight("Sprint", ["p"])
    for op in (svc.lock_spotlight, svc.unlock_spotlight):
        with pytest.raises(SpotlightError, match="Auto-spotlight"):
            op("Sprint")
        with pytest.raises(SpotlightError):
            op("Nope")


def test_renaming_a_locked_one_back_to_its_key_keeps_it_locked(root, service_for):
    svc = service_for(base(root), key_pattern=KEY)
    svc.lock_spotlight("PROJ-1")
    svc.rename_spotlight("PROJ-1", "Login")
    svc.rename_spotlight("PROJ-1", "PROJ-1")
    assert svc.spotlight("PROJ-1").summary.locked


# ---- migration -------------------------------------------------------------------------

def _named_store(root, tmp_path, includes=()):
    conn = store.connect(tmp_path / "store.sqlite")
    ingest.ingest(conn, [root.path])
    with conn:
        conn.execute("INSERT INTO spotlights (name, id) VALUES ('PROJ-1', 'sp-key'), ('Other', 'sp-other')")
        conn.execute("INSERT INTO spotlight_pins VALUES ('sp-key', 'p'), ('sp-other', 'p')")
        conn.execute("INSERT INTO spotlight_includes VALUES ('sp-other', 'sp-key')")
        conn.executemany("INSERT INTO spotlight_includes VALUES ('sp-key', ?)", [(i,) for i in includes])
    return conn


def test_a_key_named_spotlight_becomes_that_keys_locked_auto_spotlight(root, tmp_path):
    base(root)
    conn = _named_store(root, tmp_path)
    ingest.ingest(conn, [root.path], key_pattern=KEY)
    svc = QueryService(conn, "UTC", key_pattern=KEY)
    rows = {s.name: s for s in svc.spotlights()}
    assert set(rows) == {"PROJ-1", "Other"}
    a = rows["PROJ-1"]
    assert (a.kind, a.id, a.locked) == ("auto", "auto:PROJ-1", True)
    d = svc.spotlight("PROJ-1")
    assert ids(d) == {"p"} and d.picked == ("p",) and sug(d) == {"t1", "t2"}
    assert [i.name for i in svc.spotlight("Other").included] == ["PROJ-1"]   # edges to it follow
    ingest.ingest(conn, [root.path], key_pattern=KEY, rebuild=True)          # runs once, survives rebuild
    assert svc.spotlight("PROJ-1").summary.locked


def test_the_migration_fails_naming_a_key_named_spotlight_that_includes_others(root, tmp_path):
    base(root)
    conn = _named_store(root, tmp_path, includes=["sp-other"])
    with pytest.raises(SpotlightError, match="PROJ-1"):
        ingest.ingest(conn, [root.path], key_pattern=KEY)
    assert conn.execute("SELECT 1 FROM spotlights WHERE name = 'PROJ-1'").fetchone()  # nothing changed


# ---- adapters: shape only ---------------------------------------------------------------

@pytest.fixture
def cenv(root, configure):
    base(root)
    env = configure(root)
    with open(os.path.join(env["CLAUDE_COST_CONFIG_DIR"], "config.toml"), "a") as f:
        f.write('spotlight_key_pattern = "PROJ-\\\\d+"\n')
    return env


def run(argv, env):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["spotlight", *argv], env=env, now=NOW, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_cli_lock_unlock_and_state_in_list_and_show(cenv):
    assert run(["lock", "PROJ-1"], cenv)[0] == 0
    [row] = json.loads(run(["list", "--json"], cenv)[1])
    assert row["locked"] is True
    data = json.loads(run(["show", "PROJ-1", "--json"], cenv)[1])
    assert data["locked"] is True and [s["id"] for s in data["suggested"]] == ["p"]
    assert "locked" in run(["list"], cenv)[1] and "locked" in run(["show", "PROJ-1"], cenv)[1]
    assert run(["unlock", "PROJ-1"], cenv)[0] == 0
    assert json.loads(run(["list", "--json"], cenv)[1])[0]["locked"] is False
    assert "locked" not in run(["list"], cenv)[1]
    code, out, err = run(["lock", "Nope"], cenv)
    assert code != 0 and "Nope" in err


def test_web_lock_unlock_control_and_indicator(root, tmp_path):
    base(root)
    conn = store.connect(tmp_path / "web.sqlite")
    ingest.ingest(conn, [root.path])
    conn.close()

    def service():
        return QueryService(store.connect(tmp_path / "web.sqlite"), "UTC", key_pattern=KEY)
    client = create_app(service, lambda: NOW).test_client()
    url = "/spotlights/auto:PROJ-1"
    page = client.get(url).get_data(as_text=True)
    assert f'action="{url}/lock"' in page and "Locked" not in page
    assert client.post(f"{url}/lock").status_code == 302
    page = client.get(url).get_data(as_text=True)
    assert f'action="{url}/unlock"' in page and 'id="spotlight-locked"' in page
    assert 'class="badge locked"' in client.get("/spotlights").get_data(as_text=True)
    assert client.post(f"{url}/unlock").status_code == 302
    assert 'id="spotlight-locked"' not in client.get(url).get_data(as_text=True)
    assert client.post("/spotlights/sp-nope/lock").status_code in (400, 404)
