"""Seam: Project display names, through the query service and the dashboard page."""
from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req


def _names(root, service_for, projects: dict[str, str | None]):
    for i, (key, cwd) in enumerate(projects.items()):
        root.write_transcript(key, f"s{i}", [req(f"m{i}", "2026-10-29T10:00:00Z", output=1000)],
                              cwd=cwd)
    return service_for(root).project_names()


def test_plain_project_shows_last_path_component(root, service_for):
    n = _names(root, service_for, {"-Users-me-code-shop": "/Users/me/code/shop"})
    assert n["-Users-me-code-shop"] == "shop"


def test_same_folder_name_shows_parent_and_name(root, service_for):
    n = _names(root, service_for, {"-a-work-api": "/a/work/api", "-a-play-api": "/a/play/api",
                                   "-a-shop": "/a/shop"})
    assert n["-a-work-api"] == "work/api" and n["-a-play-api"] == "play/api"
    assert n["-a-shop"] == "shop"


def test_worktree_shows_repo_and_worktree_name(root, service_for):
    n = _names(root, service_for, {"-a-shop": "/a/shop",
                                   "-a-shop--claude-worktrees-fix": "/a/shop/.claude/worktrees/fix"})
    assert n["-a-shop"] == "shop"
    assert n["-a-shop--claude-worktrees-fix"] == "shop (fix)"


def test_project_without_working_directory_shows_raw_key(root, service_for):
    n = _names(root, service_for, {"-a-mystery": None})
    assert n["-a-mystery"] == "-a-mystery"


def test_dashboard_top_projects_show_display_name(root, configure):
    root.write_transcript("-Users-me-code-shop", "s1",
                          [req("m1", "2026-10-29T10:00:00Z", output=1_000_000)],
                          cwd="/Users/me/code/shop")
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    html = create_app(factory, lambda: NOW).test_client().get("/?period=last7").get_data(as_text=True)
    top = html[html.index("Top projects"):]
    assert "<td>shop</td>" in top and "-Users-me-code-shop" not in top
