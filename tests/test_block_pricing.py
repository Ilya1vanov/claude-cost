"""Block pricing: what a fixed block of tokens in a Stream cost, shared by findings."""
from datetime import date

import pytest

from claude_cost import pricing
from claude_cost.findings import FindingContext, RequestRow
from claude_cost.findings.block_pricing import Stream

MODEL, DAY = "claude-opus-5-5", date(2026, 10, 2)


def _req(ts: float, **kw) -> RequestRow:
    base = dict(message_id=f"m{ts}", session_id="s", project="p", ts=ts, day=DAY, model=MODEL,
                input=0, output=0, cache_read=10_000, write_5m=0, write_1h=0, cost=0.0,
                write_cost=0.0, cache_event="warm", speed=None, inference_geo=None, gap=100.0,
                stream_first_context=10_000)
    return RequestRow(**{**base, **kw})


def _stream(*reqs: RequestRow) -> Stream:
    return Stream(FindingContext(list(reqs), {}, {}, None, median_first_context=0.0), list(reqs))


def test_block_written_before_the_period_is_read_by_every_request_until_it_goes():
    read = pricing.find_price(MODEL, DAY).cache_read / 1_000_000
    stream = _stream(_req(100), _req(200), _req(300))
    lower, upper = stream.charge(-50, 250, 1_000)
    assert lower == pytest.approx(1_000 * 2 * read)  # requests at 100 and 200; 300 is after its end
    assert upper == pytest.approx(lower)  # no Period writer, no Cold write
    assert [c.avoidable for c in stream.covered()] == [pytest.approx(1_000 * read)] * 2


def test_block_with_no_end_is_read_to_the_end_of_the_stream():
    read = pricing.find_price(MODEL, DAY).cache_read / 1_000_000
    lower, _ = _stream(_req(100), _req(200), _req(300)).charge(-50, None, 1_000)
    assert lower == pytest.approx(1_000 * 3 * read)
