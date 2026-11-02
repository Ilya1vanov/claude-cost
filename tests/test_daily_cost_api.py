"""Seam 2: the calendar tint's per-day cost endpoint, through the Flask test client."""
import pytest

from claude_cost import cli, ingest, store
from claude_cost.query import QueryService
from claude_cost.web import create_app

from conftest import NOW  # Fri 2026-10-30
from helpers import req


@pytest.fixture
def client(root, configure):
    root.write_transcript("p", "s1", [
        req("m1", "2026-10-09T01:00:00Z", output=1_000_000),
        req("m2", "2026-10-11T01:00:00Z", output=2_000_000),
    ])
    cfg = cli.load_config(configure(root))

    def factory():
        conn = store.connect(cfg.store_path)
        ingest.ingest(conn, cfg.data_roots)
        return QueryService(conn, cfg.timezone)
    return create_app(factory, lambda: NOW).test_client()


def test_returns_a_cost_per_day_with_zero_spend_days_distinguishable(client):
    r = client.get("/api/daily-cost?start=2026-10-08&end=2026-10-12")
    assert r.status_code == 200
    days = {d["day"]: d for d in r.get_json()["days"]}
    assert list(days) == ["2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12"]
    assert days["2026-10-08"]["cost"] == 0 and days["2026-10-08"]["requests"] == 0
    assert days["2026-10-10"]["cost"] == 0 and days["2026-10-10"]["requests"] == 0
    assert days["2026-10-09"]["cost"] > 0 and days["2026-10-09"]["requests"] == 1
    assert days["2026-10-11"]["cost"] == pytest.approx(2 * days["2026-10-09"]["cost"], rel=1e-3)


@pytest.mark.parametrize("query", [
    "", "start=2026-10-08", "end=2026-10-12", "start=nope&end=2026-10-12",
    "start=2026-10-08&end=2026-13-01", "start=2026-10-12&end=2026-10-08",
])
def test_bad_dates_are_rejected(client, query):
    assert client.get("/api/daily-cost?" + query).status_code == 400


def test_span_is_capped(client):
    assert client.get("/api/daily-cost?start=2026-01-01&end=2026-02-05").status_code == 200
    assert client.get("/api/daily-cost?start=2025-01-01&end=2026-10-30").status_code == 400
