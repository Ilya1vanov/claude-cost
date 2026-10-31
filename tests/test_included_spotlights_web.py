"""Seam 2: the Included Spotlights card, include/exclude endpoints and list count via the Flask test client."""
from helpers import req
from test_spotlight_controls import M, _client, _root


def _included_client(root, tmp_path):
    _root(root)
    root.write_transcript("p", "s2", [req("m2", "2026-10-30T02:00:00Z", output=M)], branch="proj-12")
    root.write_transcript("p", "s3", [req("m3", "2026-10-30T03:00:00Z", output=M)], prompt="other")
    client, new = _client(root, tmp_path, key_pattern=r"PROJ-\d+")
    svc = new()
    svc.create_spotlight("Sprint", ["s1"], includes=["PROJ-12"])
    svc.create_spotlight("Spare", ["s3"])
    svc.close()
    return client, new


def test_included_spotlights_card_lists_rows_and_names_the_includer_in_joined_as(root, tmp_path):
    client, _ = _included_client(root, tmp_path)
    html = client.get("/spotlights/Sprint").get_data(as_text=True)
    card = html.split('id="included-spotlights"')[1].split("</section>")[0]
    assert "/spotlights/PROJ-12" in card and "$20.00" in card and "Remove" in card
    picker = card.split("<select")[1]
    assert "Spare" in picker and "PROJ-12" not in picker
    table = html.split('id="spotlight-sessions"')[1].split("</table>")[0]
    assert "Included spotlight PROJ-12" in table
    assert 'id="included-spotlights"' not in client.get("/spotlights/PROJ-12").get_data(as_text=True)


def test_include_and_exclude_endpoints_and_a_nesting_error_is_a_400(root, tmp_path):
    client, new = _included_client(root, tmp_path)
    assert client.post("/spotlights/Sprint/include", data={"spotlight": "Spare"}).status_code == 302
    assert [i.name for i in new().spotlight("Sprint").included] == ["PROJ-12", "Spare"]
    assert client.post("/spotlights/Sprint/exclude", data={"spotlight": "Spare"}).status_code == 302
    assert [i.name for i in new().spotlight("Sprint").included] == ["PROJ-12"]
    assert client.post("/spotlights/Spare/include", data={"spotlight": "Sprint"}).status_code == 400
    assert client.post("/spotlights/Sprint/include", data={"spotlight": "Sprint"}).status_code == 400


def test_create_form_accepts_included_spotlights(root, tmp_path):
    client, new = _included_client(root, tmp_path)
    assert client.post("/spotlights", data={"name": "New", "include": ["Spare"]}).status_code == 302
    assert [i.name for i in new().spotlight("New").included] == ["Spare"]
    assert client.post("/spotlights", data={"name": "Bad", "include": ["Sprint"]}).status_code == 400


def test_list_page_shows_the_include_count(root, tmp_path):
    client, _ = _included_client(root, tmp_path)
    page = client.get("/spotlights").get_data(as_text=True)
    assert "includes 1 Spotlight" in page and "includes 0" not in page
