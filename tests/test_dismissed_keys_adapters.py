"""Seam 2: Dismissed keys through the CLI and the web pages (ticket #71), shape only."""
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
    root.write_transcript("p", "bbbb2222", [req("m2", "2026-10-29T02:00:00Z", output=M)], title="PROJ-2 work")
    env = configure(root)
    with open(os.path.join(env["CLAUDE_COST_CONFIG_DIR"], "config.toml"), "a") as f:
        f.write('spotlight_key_pattern = "PROJ-\\\\d+"\n')
    return env


def run(argv, env):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["spotlight", *argv], env=env, now=NOW, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def ok(argv, env):
    code, out, err = run(argv, env)
    assert code == 0
    return out


def test_cli_dismiss_key_list_dismissed_and_restore_key(cenv):
    ok(["dismiss-key", "PROJ-1"], cenv)
    assert [s["key"] for s in json.loads(ok(["list", "--json"], cenv))] == ["PROJ-2"]
    data = json.loads(ok(["list", "--dismissed", "--json"], cenv))
    assert data == [{"key": "PROJ-1", "id": "auto:PROJ-1", "name": "PROJ-1"}]
    assert "PROJ-1" in ok(["list", "--dismissed"], cenv)
    ok(["restore-key", "PROJ-1"], cenv)
    assert ok(["list", "--dismissed"], cenv) == ""
    assert {s["key"] for s in json.loads(ok(["list", "--json"], cenv))} == {"PROJ-1", "PROJ-2"}


def test_cli_errors_are_one_line(cenv):
    code, out, err = run(["restore-key", "PROJ-1"], cenv)
    assert code != 0 and out == "" and "Dismissed key" in err
    code, out, err = run(["dismiss-key", "PROJ-404"], cenv)
    assert code != 0 and "PROJ-404" in err


def test_web_page_dismiss_and_the_dismissed_keys_list_with_restore(root, tmp_path):
    root.write_transcript("p", "s1", [req("m1", "2026-10-30T01:00:00Z", output=M)], title="PROJ-12 login")
    conn = store.connect(tmp_path / "web.sqlite")
    ingest.ingest(conn, [root.path])
    conn.close()
    client = create_app(lambda: QueryService(store.connect(tmp_path / "web.sqlite"), "UTC",
                                             key_pattern=r"PROJ-\d+"), lambda: NOW).test_client()
    page = client.get("/spotlights/PROJ-12").get_data(as_text=True)
    assert "/spotlights/auto:PROJ-12/dismiss-key" in page
    assert client.post("/spotlights/PROJ-12/dismiss-key").status_code == 302
    assert client.get("/spotlights/PROJ-12").status_code == 404
    lst = client.get("/spotlights").get_data(as_text=True)
    section = lst.split('id="dismissed-keys"')[1].split("</section>")[0]
    assert "PROJ-12" in section and "/spotlights/restore-key" in section
    assert 'href="/spotlights/auto:PROJ-12"' not in lst.split('id="spotlight-table"')[1].split("</table>")[0]
    assert client.post("/spotlights/restore-key", data={"key": "PROJ-12"}).status_code == 302
    assert client.get("/spotlights/PROJ-12").status_code == 200
    assert 'id="dismissed-keys"' not in client.get("/spotlights").get_data(as_text=True)
