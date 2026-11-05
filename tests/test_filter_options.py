"""Seam: the filter popover's server side (/filter-options, and the chips on Sessions and Breakdowns)."""
import pytest

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req

M = 1_000_000
SONNET = "claude-sonnet-5"
OPUS = "claude-opus-5-5"


def _write(root):
    root.write_transcript("proj-a", "s1aaaaaaaa-1111", [
        req("m1", "2026-10-29T10:00:00Z", output=M),
        req("m2", "2026-10-29T11:00:00Z", output=M),
        req("m4", "2026-10-29T12:00:00Z", model=SONNET, output=M),
    ], title="Login work", cwd="/code/shop")
    root.write_transcript("proj-b", "s2bbbbbbbb-2222", [
        req("m3", "2026-10-30T10:00:00Z", output=M // 2),
    ], title="Cart", cwd="/code/cart")


@pytest.fixture
def client(root, configure):
    _write(root)
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    return create_app(factory, lambda: NOW).test_client()


def _options(client, **params):
    r = client.get("/filter-options", query_string=params)
    assert r.status_code == 200
    return {o["value"]: o for o in r.get_json()["options"]}


def test_options_count_sessions_not_requests(client):
    opts = _options(client, dimension="model")
    assert opts[OPUS]["count"] == 2 and opts[SONNET]["count"] == 1  # s1 has two Opus requests


def test_options_are_narrowed_by_the_other_filters(client):
    opts = _options(client, dimension="model", project="proj-b")
    assert opts[OPUS]["count"] == 1 and SONNET not in opts


def test_selected_values_are_always_included_even_at_zero_and_do_not_narrow_themselves(client):
    opts = _options(client, dimension="model", project="proj-b", model=SONNET)
    assert opts[SONNET]["count"] == 0 and opts[OPUS]["count"] == 1


def test_q_searches_the_server_over_readable_names(client):
    assert set(_options(client, dimension="model", q="SON")) == {SONNET}
    projects = _options(client, dimension="project", q="car")
    assert list(projects) == ["proj-b"] and projects["proj-b"]["label"] == "cart"


def test_options_are_the_top_100_by_count_plus_selected(root, configure):
    root.write_transcript("p", "s1", [req(f"m{i}", "2026-10-29T10:00:00Z", output=10, git_branch=f"b{i}")
                                      for i in range(105)] +
                          [req("x", "2026-10-29T10:00:00Z", output=10, git_branch="b0")])
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    c = create_app(factory, lambda: NOW).test_client()
    plain = c.get("/filter-options?dimension=git_branch").get_json()["options"]
    assert len(plain) == 100 and plain[0]["value"] == "b0"
    picked = c.get("/filter-options?dimension=git_branch&git_branch=b99").get_json()["options"]
    assert len(picked) == 101 and "b99" in {o["value"] for o in picked}


def test_unknown_dimension_is_a_400(client):
    assert client.get("/filter-options?dimension=nope").status_code == 400


@pytest.mark.parametrize("page", ["/sessions", "/breakdowns"])
def test_pages_have_the_filter_button_and_no_old_form(client, page):
    html = client.get(page).get_data(as_text=True)
    assert "Filter</button>" in html
    assert "<details" not in html and "<datalist" not in html


@pytest.mark.parametrize("page", ["/sessions", "/breakdowns"])
def test_chips_read_model_a_b_with_readable_names_and_a_remove_link(client, page):
    html = client.get(f"{page}?model={OPUS}&model={SONNET}&project=proj-a&session_name=login").get_data(as_text=True)
    assert f'title="Model: {OPUS}, {SONNET}"' in html and f"<strong>{OPUS}, {SONNET}</strong>" in html
    assert "<strong>shop</strong>" in html  # project name, not the key
    assert "<strong>“login”</strong>" in html
    assert f"{page}?" in html and 'aria-label="Remove Model filter"' in html


def test_a_chip_with_many_or_long_values_is_shortened_and_keeps_the_full_list_on_hover(client):
    html = client.get("/sessions?tool=A&tool=B&tool=C").get_data(as_text=True)
    assert "<strong>A +2</strong>" in html and 'title="Tool: A, B, C"' in html
    long = "x" * 41
    html = client.get(f"/sessions?tool={long}").get_data(as_text=True)
    assert f"<strong>{long}</strong>" in html  # one value is never cut
    html = client.get(f"/sessions?tool={'y' * 25}&tool={'z' * 25}").get_data(as_text=True)
    assert f"<strong>{'y' * 25} +1</strong>" in html


def test_each_filter_is_a_single_chip(client):
    html = client.get(f"/sessions?model={OPUS}&model={SONNET}").get_data(as_text=True)
    assert html.count('class="chip"') == 1
