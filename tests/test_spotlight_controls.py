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
    assert table.count(">Key match<") == 1 and table.count(">Picked<") == 1
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


def _two_spotlight_client(root, tmp_path):
    _root(root)
    root.write_transcript("p", "s2", [req("m2", "2026-10-30T02:00:00Z", output=M)],
                          branch="feature/proj-12-x")
    root.write_subagent_transcript("p", "s1", "agent-abc123", [req("m3", "2026-10-30T03:00:00Z", output=M)],
                                   meta={"agentType": "Explore", "description": "Scan the repo"})
    client, new = _client(root, tmp_path, key_pattern=r"PROJ-\d+")
    svc = new()
    svc.create_spotlight("login", ["s1"])
    svc.close()
    return client


def test_sections_run_in_order_and_delete_is_the_last_card_on_user_spotlights_only(root, tmp_path):
    client = _two_spotlight_client(root, tmp_path)
    page = client.get("/spotlights/login").get_data(as_text=True)
    order = ["<h1>", "Cost per day", "Cost components", 'id="spotlight-sessions"', 'id="spotlight-days"']
    positions = [page.index(m) for m in order]
    assert positions == sorted(positions)
    assert "Delete Spotlight" not in page.split("Cost per day", 1)[0]
    assert page.rindex("Delete Spotlight") > page.index('id="spotlight-days"')
    assert 'class="card danger-zone"' in page.rsplit("<section", 1)[1]
    auto = client.get("/spotlights/PROJ-12").get_data(as_text=True)
    assert "Delete Spotlight" not in auto


def test_related_excluded_sits_between_days_and_delete(root, tmp_path):
    _root(root)
    root.write_transcript("p", "fk", [req("m1", "2026-10-30T01:00:00Z", output=M, forked_from="s1"),
                                      req("m4", "2026-10-30T04:00:00Z", output=M)], prompt="go on")
    root.write_subagent_transcript("p", "s1", "a1", [req("m3", "2026-10-30T03:00:00Z", output=M)])
    client, new = _client(root, tmp_path)
    svc = new()
    svc.create_spotlight("login", ["s1"])
    svc.close()
    page = client.get("/spotlights/login").get_data(as_text=True)
    assert page.index('id="spotlight-days"') < page.index('id="spotlight-related"') < page.rindex("Delete Spotlight")


def test_also_in_column_only_when_a_session_is_in_another_spotlight(root, tmp_path):
    client = _two_spotlight_client(root, tmp_path)
    table = client.get("/spotlights/login").get_data(as_text=True).split('id="spotlight-sessions"')[1].split("</table>")[0]
    assert "Also in" not in table
    _, new = _client(root, tmp_path, key_pattern=r"PROJ-\d+")
    svc = new()
    svc.create_spotlight("other", ["s2"])
    svc.close()
    table = client.get("/spotlights/PROJ-12").get_data(as_text=True).split('id="spotlight-sessions"')[1].split("</table>")[0]
    assert "Also in" in table and ">other<" in table


def test_subagents_card_is_gone_but_cost_stays_in_the_columns(root, tmp_path):
    client = _two_spotlight_client(root, tmp_path)
    page = client.get("/spotlights/login").get_data(as_text=True)
    assert 'id="spotlight-subagents"' not in page and "<h2>Subagents</h2>" not in page
    assert "of which Subagents" in page and "$20.00 · 1 run" in page.split('id="spotlight-days"')[1]


def test_auto_spotlights_hint_only_without_key_pattern(root, tmp_path):
    _root(root)
    off, _ = _client(root, tmp_path)
    page = off.get("/spotlights").get_data(as_text=True)
    assert "Auto-spotlights are off" in page and "spotlight_key_pattern" in page
    on, _ = _client(root, tmp_path, key_pattern=r"PROJ-\d+")
    assert "Auto-spotlights are off" not in on.get("/spotlights").get_data(as_text=True)


def test_days_table_has_subagent_summary_cell_and_sessions_costliest_first(root, tmp_path):
    root.write_transcript("p", "cheap", [req("m1", "2026-10-30T01:00:00Z", output=M // 10)], title="CHEAP PROJ-1")
    root.write_transcript("p", "dear", [req("m2", "2026-10-30T01:00:00Z", output=M)], title="DEAR PROJ-1")
    root.write_subagent_transcript("p", "dear", "a1", [req("m3", "2026-10-30T02:00:00Z", output=M)],
                                   meta={"agentType": "Explore"})
    client, _ = _client(root, tmp_path, key_pattern=r"PROJ-\d+")
    page = client.get("/spotlights/PROJ-1").get_data(as_text=True)
    days = page[page.index('id="spotlight-days"'):]
    assert "$20.00 · 1 run · all Explore" in days
    assert days.index("DEAR") < days.index("CHEAP")


def test_search_endpoint_serves_ranked_candidates_as_json(root, tmp_path):
    _root(root)
    root.write_transcript("p", "s2", [req("m2", "2026-10-30T02:00:00Z", output=M)], prompt="billing page")
    client, new = _client(root, tmp_path)
    svc = new()
    svc.create_spotlight("login", ["s1"])
    svc.close()
    got = client.get("/api/session-search", query_string={"q": "bill", "name": "login"})
    assert got.status_code == 200 and got.is_json
    assert got.get_json() == {"results": [
        {"id": "s2", "short_id": "s2", "label": "billing page", "cost": 20.0, "kind": None, "suggested": False}]}
    skipped = client.get("/api/session-search", query_string={"q": "bill", "exclude": ["s2"]})
    assert skipped.get_json() == {"results": []}
    assert client.get("/api/session-search").get_json() == {"results": []}  # empty query, no Forks


def test_spotlight_header_is_only_a_batch_picker_with_no_saved_chips(root, tmp_path):
    _root(root)
    root.write_transcript("p", "s2", [req("m2", "2026-10-30T02:00:00Z", output=M)],
                          branch="feature/proj-12-x", prompt="ticket work")
    client, _ = _client(root, tmp_path, key_pattern=r"PROJ-\d+")
    client.post("/spotlights/PROJ-12/add", data={"session": "s1"})
    page = client.get("/spotlights/PROJ-12").get_data(as_text=True)
    head = page.split("Cost per day", 1)[0]
    assert "<select" not in page and "Pin a Session" not in page
    assert 'data-search-url="/api/session-search"' in head and 'data-name="auto:PROJ-12"' in head
    assert 'data-mode="collect"' in head and 'action="/spotlights/auto:PROJ-12/add"' in head
    assert '<span class="chip"' not in head and "chip-x" not in head and "login work" not in head
    assert "picker-submit" in head
    js = client.get("/static/picker.js").get_data(as_text=True)
    assert "Suggested: Forks" in js and "sessions" in js
    table = page.split('id="spotlight-sessions"')[1].split("</table>")[0]  # saved members live here
    assert "login work" in table and "/spotlights/auto:PROJ-12/remove" in table and "Key match" in table


def test_add_endpoint_takes_several_ids_in_one_post(root, tmp_path):
    _root(root)
    root.write_transcript("p", "s2", [req("m2", "2026-10-30T02:00:00Z", output=M)], prompt="billing")
    root.write_transcript("p", "s3", [req("m3", "2026-10-30T03:00:00Z", output=M)], prompt="search")
    client, new = _client(root, tmp_path)
    assert client.post("/spotlights/login/add", data={"session": ["s1", "s2", "s3"]}).status_code == 302
    assert {s.session_id for s in new().spotlight("login").sessions} == {"s1", "s2", "s3"}


def test_chip_remove_label_is_plain_text_and_chips_are_capped(root, tmp_path):
    client, _ = _client(_root(root), tmp_path)
    css = client.get("/static/app.css").get_data(as_text=True)
    chip = css.split(".picker .chip {")[1].split("}")[0]
    assert "text-overflow: ellipsis" in chip and "max-width" in chip and "white-space: nowrap" in chip
    js = client.get("/static/picker.js").get_data(as_text=True)
    assert 'setAttribute("aria-label", "Remove " + c.label)' in js and "chip.title = c.label" in js


def test_new_spotlight_form_uses_the_picker(root, tmp_path):
    _root(root)
    client, _ = _client(root, tmp_path)
    page = client.get("/spotlights").get_data(as_text=True)
    form = page.split("<h2>New Spotlight</h2>", 1)[1]
    assert 'data-search-url="/api/session-search"' in form and 'data-mode="collect"' in form
    assert "/static/picker.js" in page
