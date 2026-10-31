"""Seam 2: Spotlight page controls (Remove rule, Delete placement, Auto-spotlights hint) via the Flask test client."""
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


def test_remove_rule_button_empties_rule_leaving_pinned_only(root, tmp_path):
    _root(root)
    client, new = _client(root, tmp_path)
    svc = new()
    svc.save_spotlight("login", "login")
    svc.pin_session("login", "s1")
    svc.close()
    page = client.get("/spotlights/login").get_data(as_text=True)
    assert "Remove rule" in page and "Save rule" in page
    # the Remove rule button submits an empty rule to the save-rule action
    assert 'name="rule" value=""' in page
    client.post("/spotlights/login/rule", data={"rule": ""})
    svc = new()
    d = svc.spotlight("login")
    svc.close()
    assert d.summary.kind == "pinned" and not d.summary.rule


def test_delete_sits_next_to_heading_for_rule_spotlights_only(root, tmp_path):
    _root(root)
    root.write_transcript("p", "s2", [req("m2", "2026-10-30T02:00:00Z", output=M)],
                          branch="feature/proj-12-x")
    client, new = _client(root, tmp_path, key_pattern=r"PROJ-\d+")
    svc = new()
    svc.save_spotlight("login", "login")
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
