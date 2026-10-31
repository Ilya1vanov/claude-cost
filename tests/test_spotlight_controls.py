"""Seam 2: Spotlight page controls (Picked sessions, Delete placement, Auto-spotlights hint) via the Flask test client."""
from claude_cost import ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req

M = 1_000_000


def _client(root, tmp_path, key_pattern=None):
    conn = store.connect(tmp_path / "web.sqlite")
    ingest.ingest(conn, [root.path])
    conn.close()

    def service():
        return QueryService(store.connect(tmp_path / "web.sqlite"), "UTC", key_pattern=key_pattern)
    return create_app(service, lambda: NOW).test_client(), service


def _root(root):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=M)], prompt="login work")
    return root


def test_page_has_no_rule_controls_and_new_form_has_no_rule_field(root, tmp_path):
    _root(root)
    client, new = _client(root, tmp_path)
    svc = new()
    svc.create_spotlight("login", ["s1"])
    svc.close()
    page = client.get("/spotlights/login").get_data(as_text=True)
    assert "rule" not in page.lower() and "Save rule" not in page
    assert client.post("/spotlights/login/rule", data={"rule": ""}).status_code in (404, 405)
    assert 'name="rule"' not in client.get("/spotlights").get_data(as_text=True)


def test_add_and_remove_session_endpoints_and_joined_as_column(root, tmp_path):
    _root(root)
    root.write_transcript("p", "s2", [req("m2", "2026-10-30T02:00:00Z", output=M)],
                          branch="feature/proj-12-x", prompt="ticket work")
    client, _ = _client(root, tmp_path, key_pattern=r"PROJ-\d+")
    assert client.post("/spotlights/PROJ-12/add", data={"session": "s1"}).status_code == 302
    html = client.get("/spotlights/PROJ-12").get_data(as_text=True)
    table = html.split('id="spotlight-sessions"')[1].split("</table>")[0]
    assert "Joined as" in table and ">Link<" not in table
    assert table.count("Key match") == 1 and table.count("Picked") == 1
    assert client.post("/spotlights/PROJ-12/remove", data={"session": "s2"}).status_code == 400
    assert client.post("/spotlights/PROJ-12/remove", data={"session": "s1"}).status_code == 302
    table = client.get("/spotlights/PROJ-12").get_data(as_text=True).split('id="spotlight-sessions"')[1].split("</table>")[0]
    assert "Picked" not in table and "login work" not in table


def test_creating_with_a_name_only_and_with_ids(root, tmp_path):
    _root(root)
    client, new = _client(root, tmp_path)
    assert client.post("/spotlights", data={"name": "blank"}).status_code == 302
    assert client.post("/spotlights", data={"name": "one", "session": ["s1"]}).status_code == 302
    svc = new()
    assert svc.spotlight("blank").sessions == ()
    assert [s.session_id for s in svc.spotlight("one").sessions] == ["s1"]
    assert client.post("/spotlights", data={"name": " "}).status_code == 400


def test_list_page_names_the_kinds_picked_and_auto_spotlight(root, tmp_path):
    _root(root)
    root.write_transcript("p", "s2", [req("m2", "2026-10-30T02:00:00Z", output=M)], branch="proj-12")
    client, new = _client(root, tmp_path, key_pattern=r"PROJ-\d+")
    svc = new()
    svc.create_spotlight("login", ["s1"])
    svc.close()
    page = client.get("/spotlights").get_data(as_text=True)
    assert ">Picked<" in page and ">Auto-spotlight<" in page
    assert "rule" not in page.lower() and "pinned" not in page.lower()


def test_delete_sits_next_to_heading_for_user_spotlights_only(root, tmp_path):
    _root(root)
    root.write_transcript("p", "s2", [req("m2", "2026-10-30T02:00:00Z", output=M)],
                          branch="feature/proj-12-x")
    client, new = _client(root, tmp_path, key_pattern=r"PROJ-\d+")
    svc = new()
    svc.create_spotlight("login", ["s1"])
    svc.close()
    page = client.get("/spotlights/login").get_data(as_text=True)
    assert "Delete Spotlight" in page.split("<h1>", 1)[1].split("Cost per day", 1)[0]
    auto = client.get("/spotlights/PROJ-12").get_data(as_text=True)
    assert "Delete Spotlight" not in auto


def test_auto_spotlights_hint_only_without_key_pattern(root, tmp_path):
    _root(root)
    off, _ = _client(root, tmp_path)
    page = off.get("/spotlights").get_data(as_text=True)
    assert "Auto-spotlights are off" in page and "spotlight_key_pattern" in page
    on, _ = _client(root, tmp_path, key_pattern=r"PROJ-\d+")
    assert "Auto-spotlights are off" not in on.get("/spotlights").get_data(as_text=True)
