"""`serve` reuses an open dashboard tab: focus and reload it instead of opening another."""
from __future__ import annotations

import io

from claude_cost import cli

from conftest import NOW

URL = "http://127.0.0.1:8765/"


def serve(env, *argv, probe=lambda url: None, focus=lambda url: False, extra_env=None):
    calls = []
    def run_server(app, host, port, on_ready):
        calls.append("bind")
        on_ready()
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["serve", *argv], env={**env, **(extra_env or {})}, now=NOW, out=out, err=err,
                    run_server=run_server, open_browser=lambda url: calls.append(("open", url)),
                    focus_tab=lambda url: calls.append(("focus", url)) or focus(url),
                    probe_server=probe)
    return code, calls, out.getvalue(), err.getvalue()


def test_open_tab_is_focused_after_binding_and_no_new_tab_opens(root, configure):
    code, calls, _, _ = serve(configure(root), focus=lambda url: True)
    assert code == 0 and calls == ["bind", ("focus", URL)]


def test_no_open_tab_opens_a_new_one_after_binding(root, configure):
    code, calls, _, _ = serve(configure(root))
    assert code == 0 and calls == ["bind", ("focus", URL), ("open", URL)]


def test_running_server_reuses_its_tab_without_a_second_server(root, configure):
    code, calls, out, _ = serve(configure(root), probe=lambda url: "claude-cost", focus=lambda url: True)
    assert code == 0 and calls == [("focus", URL)]
    assert f"Already serving {URL}" in out


def test_port_held_by_another_program_fails_without_a_tab(root, configure):
    code, calls, _, err = serve(configure(root), probe=lambda url: "<html>something else</html>")
    assert code == 1 and calls == []
    assert "port 8765 is in use by another program" in err


def test_browser_none_neither_focuses_nor_opens(root, configure):
    env = configure(root)
    for argv, extra in ((["--browser=none"], None), ([], {"BROWSER": "none"})):
        code, calls, _, _ = serve(env, *argv, extra_env=extra, focus=lambda url: True)
        assert code == 0 and calls == ["bind"]
        code, calls, _, _ = serve(env, *argv, extra_env=extra, probe=lambda url: "claude-cost")
        assert code == 0 and calls == []


def test_healthz_names_the_app(root, configure):
    from claude_cost.web import create_app
    client = create_app(lambda: None, lambda: NOW).test_client()
    page = client.get("/healthz")
    assert page.status_code == 200 and page.get_data(as_text=True) == "claude-cost"
