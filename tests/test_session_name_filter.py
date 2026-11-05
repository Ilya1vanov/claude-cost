"""Seam: the Session name filter (service filtering, /filter-options suggestions, chip)."""
import pytest

from claude_cost import cli, ingest, store
from claude_cost.explore import ExploreSpec
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW
from helpers import req

M = 1_000_000


def _write(root):
    root.write_transcript("proj-a", "s1aaaaaaaa-1111", [
        req("m1", "2026-10-29T10:00:00Z", output=M),
        req("m2", "2026-10-29T11:00:00Z", output=M),
    ], title="Login work", cwd="/code/shop")
    root.write_transcript("proj-a", "s2bbbbbbbb-2222", [
        req("m3", "2026-10-30T10:00:00Z", output=M // 2),
    ], prompt="Investigate the nightly export", cwd="/code/shop")
    root.write_transcript("proj-b", "s3cccccccc-3333", [
        req("m4", "2026-10-30T11:00:00Z", output=M // 4),
    ], title="Cart", cwd="/code/cart")


@pytest.fixture
def factory(root, configure):
    _write(root)
    cfg = cli.load_config(configure(root))

    def make():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    return make


@pytest.fixture
def svc(factory):
    return factory()


@pytest.fixture
def client(factory):
    return create_app(factory, lambda: NOW).test_client()


def _sessions(svc, *names):
    spec = ExploreSpec("date", {"session_name": tuple(names)})
    return {r.session_id for r in svc.sessions(spec).rows}


def test_matches_part_of_the_label_ignoring_case(svc):
    assert _sessions(svc, "LOGIN w") == {"s1aaaaaaaa-1111"}


def test_matches_the_first_prompt_when_there_is_no_custom_title(svc):
    assert _sessions(svc, "nightly") == {"s2bbbbbbbb-2222"}


def test_matches_part_of_the_session_id(svc):
    assert _sessions(svc, "CCCC") == {"s3cccccccc-3333"}


def test_several_texts_match_any_and_no_match_keeps_nothing(svc):
    assert _sessions(svc, "cart", "login") == {"s1aaaaaaaa-1111", "s3cccccccc-3333"}
    assert _sessions(svc, "zzz") == set()


def test_filters_breakdowns_too(svc):
    spec = ExploreSpec("project", {"session_name": ("login",)})
    assert {r.value: r.requests for r in svc.explore(spec).rows} == {"proj-a": 2}


def test_exact_id_session_filter_is_gone(client):
    assert ExploreSpec.from_query([("session", "s1aaaaaaaa-1111")]).filters == {}
    assert ExploreSpec.from_query([("session_name", "x")]).filters == {"session_name": ("x",)}
    assert client.get("/filter-options?dimension=session").status_code == 400
    html = client.get("/sessions?session=s1aaaaaaaa-1111").get_data(as_text=True)
    assert "Login work" in html and "Cart" in html  # not narrowed


def test_suggestions_are_sessions_whose_name_or_id_contains_the_text(client):
    opts = client.get("/filter-options?dimension=session_name&q=LOG").get_json()["options"]
    assert [(o["value"], o["label"], o["id"]) for o in opts] == [
        ("Login work", "Login work", "s1aaaaaaaa-1111")]
    by_id = client.get("/filter-options?dimension=session_name&q=3ccc").get_json()["options"]
    assert [o["id"] for o in by_id] == ["s3cccccccc-3333"]
    everything = client.get("/filter-options?dimension=session_name").get_json()["options"]
    assert [o["id"] for o in everything] == ["s1aaaaaaaa-1111", "s2bbbbbbbb-2222", "s3cccccccc-3333"]


def test_suggestions_match_a_substring_not_a_subsequence(client):
    def ids(q):
        return [o["id"] for o in client.get("/filter-options", query_string={
            "dimension": "session_name", "q": q}).get_json()["options"]]
    assert ids("ogin w") == ["s1aaaaaaaa-1111"]
    assert ids("lgnk") == [] and ids("lgn") == []  # letters of "Login work" in order, but not contiguous


def test_suggestions_are_narrowed_by_the_other_filters(client):
    opts = client.get("/filter-options?dimension=session_name&project=proj-b").get_json()["options"]
    assert [o["id"] for o in opts] == ["s3cccccccc-3333"]


def test_popover_lists_session_name_not_session_and_the_chip_quotes_the_text(client):
    html = client.get("/sessions?session_name=timel").get_data(as_text=True)
    assert '"key": "session_name"' in html and '"label": "Session name"' in html
    assert '"key": "session"' not in html
    assert "Session name:" in html and "<strong>“timel”</strong>" in html
