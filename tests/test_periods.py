"""Seam 1: period modes and comparisons through the query service."""
from datetime import date, datetime, timezone

import pytest

from claude_cost import store
from claude_cost.query import QueryService

from helpers import req

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)  # a Friday


def _spend(root, project, session, day, mid, output=1_000_000, hour=10):
    """$20 per 1M output tokens at the fixture model's price."""
    root.write_transcript(project, session,
                          [req(mid, f"{day}T{hour:02d}:00:00Z", output=output)])


def test_last7_including_today_vs_previous_7_days(root, service_for):
    _spend(root, "p", "s1", "2026-10-30", "a")   # today, current
    _spend(root, "p", "s2", "2026-10-24", "b")   # first day of current (24..30)
    _spend(root, "p", "s3", "2026-10-23", "c")   # last day of baseline (17..23)
    _spend(root, "p", "s4", "2026-10-17", "d")   # first day of baseline
    _spend(root, "p", "s5", "2026-10-16", "e")   # outside both
    c = service_for(root).period_comparison("last7", NOW)
    assert (c.current.start, c.current.end) == (date(2026, 10, 24), date(2026, 10, 30))
    assert (c.baseline.start, c.baseline.end) == (date(2026, 10, 17), date(2026, 10, 23))
    assert (c.current_cost, c.baseline_cost) == (pytest.approx(40.0), pytest.approx(40.0))
    assert not c.partial


def test_last7_excluding_today_ends_yesterday(root, service_for):
    _spend(root, "p", "s1", "2026-10-30", "a")   # today: excluded
    _spend(root, "p", "s2", "2026-10-29", "b")
    _spend(root, "p", "s3", "2026-10-23", "c")   # first day of current when today excluded
    _spend(root, "p", "s4", "2026-10-22", "d")   # last day of baseline
    c = service_for(root).period_comparison("last7", NOW, include_today=False)
    assert (c.current.start, c.current.end) == (date(2026, 10, 23), date(2026, 10, 29))
    assert (c.baseline.start, c.baseline.end) == (date(2026, 10, 16), date(2026, 10, 22))
    assert c.current_cost == pytest.approx(40.0)
    assert c.baseline_cost == pytest.approx(20.0)
    assert not c.partial


def test_deltas_in_dollars_and_percent(root, service_for):
    _spend(root, "p", "s1", "2026-10-29", "a", output=3_000_000)   # $60 now
    _spend(root, "p", "s2", "2026-10-20", "b", output=2_000_000)   # $40 before
    c = service_for(root).period_comparison("last7", NOW)
    assert c.delta_cost == pytest.approx(20.0)
    assert c.delta_pct == pytest.approx(50.0)


def test_percent_is_none_when_baseline_is_zero(root, service_for):
    _spend(root, "p", "s1", "2026-10-29", "a")
    c = service_for(root).period_comparison("last7", NOW)
    assert c.baseline_cost == 0 and c.delta_pct is None and c.delta_cost == pytest.approx(20.0)


def test_calendar_week_is_partial_and_compares_equal_elapsed_days(root, service_for):
    # Fri 2026-10-30: week = Mon 26..Fri 30 (5 of 7 days), baseline Mon 19..Fri 23
    _spend(root, "p", "s1", "2026-10-26", "a")   # Monday, current
    _spend(root, "p", "s2", "2026-10-30", "b")
    _spend(root, "p", "s3", "2026-10-23", "c")   # baseline Friday: counts
    _spend(root, "p", "s4", "2026-10-24", "d")   # baseline Saturday: beyond elapsed days
    _spend(root, "p", "s5", "2026-10-25", "e")   # baseline Sunday: beyond elapsed days
    c = service_for(root).period_comparison("week", NOW)
    assert (c.current.start, c.current.end) == (date(2026, 10, 26), date(2026, 10, 30))
    assert (c.baseline.start, c.baseline.end) == (date(2026, 10, 19), date(2026, 10, 23))
    assert c.partial and c.elapsed_days == 5 and c.period_days == 7
    assert c.current_cost == pytest.approx(40.0)
    assert c.baseline_cost == pytest.approx(20.0)


def test_calendar_week_on_sunday_is_complete(root, service_for):
    sunday = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
    _spend(root, "p", "s1", "2026-10-26", "a")
    _spend(root, "p", "s2", "2026-10-25", "b")   # previous Sunday, in baseline
    c = service_for(root).period_comparison("week", sunday)
    assert (c.current.start, c.current.end) == (date(2026, 10, 26), date(2026, 11, 1))
    assert (c.baseline.start, c.baseline.end) == (date(2026, 10, 19), date(2026, 10, 25))
    assert not c.partial
    assert c.baseline_cost == pytest.approx(20.0)


def test_calendar_week_on_monday_is_one_day_partial(root, service_for):
    monday = datetime(2026, 10, 26, 0, 30, tzinfo=timezone.utc)
    c = service_for(root).period_comparison("week", monday)
    assert c.current.start == c.current.end == date(2026, 10, 26)
    assert c.baseline.start == c.baseline.end == date(2026, 10, 19)
    assert c.partial and c.elapsed_days == 1


def test_calendar_month_partial_vs_same_elapsed_days_of_previous_month(root, service_for):
    _spend(root, "p", "s1", "2026-10-01", "a")
    _spend(root, "p", "s2", "2026-09-30", "b")   # last baseline day: counts
    _spend(root, "p", "s3", "2026-09-01", "c")
    _spend(root, "p", "s4", "2026-08-31", "d")   # before baseline
    c = service_for(root).period_comparison("month", NOW)
    assert (c.current.start, c.current.end) == (date(2026, 10, 1), date(2026, 10, 30))
    assert (c.baseline.start, c.baseline.end) == (date(2026, 9, 1), date(2026, 9, 30))
    assert c.partial and c.elapsed_days == 30 and c.period_days == 31
    assert c.current_cost == pytest.approx(20.0)
    assert c.baseline_cost == pytest.approx(40.0)


def test_month_baseline_is_clamped_to_shorter_previous_month(service_for, root):
    march31 = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    c = service_for(root).period_comparison("month", march31)
    assert (c.baseline.start, c.baseline.end) == (date(2026, 2, 1), date(2026, 2, 28))
    assert not c.partial  # 31 of 31 days


def test_complete_month_is_not_partial(root, service_for):
    last_day = datetime(2026, 10, 31, 23, 0, tzinfo=timezone.utc)
    c = service_for(root).period_comparison("month", last_day)
    assert not c.partial and c.elapsed_days == c.period_days == 31


def test_period_edges_follow_the_local_time_zone(root, tmp_path, service_for):
    # 2026-10-26T06:30Z is Sunday 2026-10-25 23:30 in Los Angeles (PDT, UTC-7):
    # the previous week locally, but Monday (current week) in UTC.
    _spend(root, "p", "s1", "2026-10-26", "a", hour=6)
    service_for(root)
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)  # Sunday: full week
    la = QueryService(store.connect(tmp_path / "store.sqlite"), "America/Los_Angeles")
    utc = QueryService(store.connect(tmp_path / "store.sqlite"), "UTC")
    assert la.period_comparison("week", now).current_cost == 0
    assert la.period_comparison("week", now).baseline_cost == pytest.approx(20.0)
    assert utc.period_comparison("week", now).current_cost == pytest.approx(20.0)


def test_today_comes_from_the_local_clock_not_utc(root, tmp_path, service_for):
    # 2026-10-31T02:00Z is still Fri 2026-10-30 in Los Angeles
    service_for(root)
    now = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)
    la = QueryService(store.connect(tmp_path / "store.sqlite"), "America/Los_Angeles")
    assert la.period_comparison("last7", now).current.end == date(2026, 10, 30)


def test_cost_components_split_current_and_baseline(root, service_for):
    root.write_transcript("p", "s1", [req("a", "2026-10-29T10:00:00Z", input=1_000_000,
                                           output=1_000_000, cache_read=1_000_000,
                                           write_5m=1_000_000, write_1h=1_000_000)])
    c = service_for(root).period_comparison("last7", NOW)
    parts = {x.name: x for x in c.components}
    assert list(parts) == ["input", "output", "cache write (5m)", "cache write (1h)", "cache read"]
    assert sum(x.current for x in c.components) == pytest.approx(c.current_cost)
    assert all(x.current > 0 and x.baseline == 0 for x in c.components)


def test_top_n_sessions_and_projects_ranked_by_period_cost(root, service_for):
    _spend(root, "big", "s1", "2026-10-29", "a", output=3_000_000)
    _spend(root, "small", "s2", "2026-10-29", "b", output=1_000_000)
    _spend(root, "small", "s3", "2026-10-28", "c", output=1_000_000)
    _spend(root, "big", "old", "2026-10-01", "d", output=9_000_000)  # outside the period
    c = service_for(root).period_comparison("last7", NOW, top_n=2)
    assert [(s.key, round(s.cost)) for s in c.top_sessions] == [("s1", 60), ("s2", 20)]
    assert [(p.key, round(p.cost), p.requests) for p in c.top_projects] == \
        [("big", 60, 1), ("small", 40, 2)]
    assert len(service_for(root).period_comparison("last7", NOW, top_n=1).top_projects) == 1


def test_unknown_mode_is_rejected(root, service_for):
    with pytest.raises(ValueError):
        service_for(root).period_comparison("year", NOW)


# --- Custom period -------------------------------------------------------------------------

from claude_cost.query import Period  # noqa: E402


def test_custom_period_and_its_baseline_of_equal_length(root, service_for):
    _spend(root, "p", "s1", "2026-10-12", "a")   # first day of current (12..16)
    _spend(root, "p", "s2", "2026-10-16", "b")   # last day of current
    _spend(root, "p", "s3", "2026-10-11", "c")   # last day of baseline (7..11)
    _spend(root, "p", "s4", "2026-10-07", "d")   # first day of baseline
    _spend(root, "p", "s5", "2026-10-06", "e")   # outside both
    _spend(root, "p", "s6", "2026-10-17", "f")   # after the period
    c = service_for(root).period_comparison(
        "custom", NOW, custom=Period(date(2026, 10, 12), date(2026, 10, 16)))
    assert c.mode == "custom"
    assert (c.current.start, c.current.end) == (date(2026, 10, 12), date(2026, 10, 16))
    assert (c.baseline.start, c.baseline.end) == (date(2026, 10, 7), date(2026, 10, 11))
    assert (c.current_cost, c.baseline_cost) == (pytest.approx(40.0), pytest.approx(40.0))
    assert not c.partial


def test_custom_one_day_period_baseline_is_the_day_before(root, service_for):
    _spend(root, "p", "s1", "2026-10-20", "a")
    _spend(root, "p", "s2", "2026-10-19", "b", output=2_000_000)
    c = service_for(root).period_comparison(
        "custom", NOW, custom=Period(date(2026, 10, 20), date(2026, 10, 20)))
    assert (c.baseline.start, c.baseline.end) == (date(2026, 10, 19), date(2026, 10, 19))
    assert (c.current_cost, c.baseline_cost) == (pytest.approx(20.0), pytest.approx(40.0))


def test_custom_period_crossing_a_month_boundary(root, service_for):
    _spend(root, "p", "s1", "2026-09-29", "a")   # first day of current (9-29..10-02)
    _spend(root, "p", "s2", "2026-10-02", "b")
    _spend(root, "p", "s3", "2026-09-28", "c")   # last day of baseline (9-25..9-28)
    c = service_for(root).period_comparison(
        "custom", NOW, custom=Period(date(2026, 9, 29), date(2026, 10, 2)))
    assert (c.baseline.start, c.baseline.end) == (date(2026, 9, 25), date(2026, 9, 28))
    assert (c.current_cost, c.baseline_cost) == (pytest.approx(40.0), pytest.approx(20.0))


def test_custom_period_ending_today_is_not_partial(root, service_for):
    c = service_for(root).period_comparison(
        "custom", NOW, custom=Period(date(2026, 10, 1), date(2026, 10, 30)))
    assert not c.partial and c.elapsed_days == c.period_days == 30


def test_cache_card_spotlights_and_findings_use_the_same_custom_period(root, service_for):
    _spend(root, "p", "s1", "2026-10-14", "a")   # in current (12..16)
    _spend(root, "p", "s2", "2026-10-09", "b")   # in baseline (7..11)
    _spend(root, "p", "s3", "2026-10-20", "c")   # outside both
    for sid, day, mid, kw in (("c1", "14", "ca", {"write_5m": 100_000}),   # current
                              ("c2", "09", "cb", {"write_5m": 100_000}),   # baseline
                              ("c3", "20", "cc", {"write_5m": 100_000})):  # outside both
        root.write_transcript("p", sid, [req(mid, f"2026-10-{day}T10:00:00Z", **kw)])
    svc = service_for(root)
    svc.create_spotlight("Sprint", ["s1", "s2", "s3"])
    custom = Period(date(2026, 10, 12), date(2026, 10, 16))
    card = svc.cache_card("custom", NOW, custom=custom)
    assert card.mode == "custom"
    one_write = 100_000 * 5 / 1e6    # 5m cache write at the fixture model's $5/M
    assert card.current.write_5m_cost == pytest.approx(one_write)
    assert card.baseline.write_5m_cost == pytest.approx(one_write)
    ranks = svc.top_spotlights("custom", NOW, custom=custom)
    assert [(r.summary.name, round(r.period_cost)) for r in ranks] == [("Sprint", 20)]
    all_ids = {f.id for f in svc.findings(custom.start, custom.end)}
    got = {f.id for f in svc.top_findings("custom", NOW, custom=custom)}
    assert got <= all_ids


@pytest.mark.parametrize("start,end", [
    (date(2026, 10, 16), date(2026, 10, 12)),   # end before start
    (date(2026, 10, 31), date(2026, 11, 2)),    # start after today
])
def test_invalid_custom_bounds_raise(root, service_for, start, end):
    svc = service_for(root)
    with pytest.raises(ValueError):
        svc.period_comparison("custom", NOW, custom=Period(start, end))
    with pytest.raises(ValueError):
        svc.cache_card("custom", NOW, custom=Period(start, end))


def test_custom_mode_without_dates_raises(root, service_for):
    with pytest.raises(ValueError):
        service_for(root).period_comparison("custom", NOW)


def test_first_request_day_is_the_local_day_of_the_earliest_request(root, service_for):
    _spend(root, "p", "s1", "2026-10-20", "a")
    _spend(root, "p", "s2", "2026-10-05", "b", hour=0)
    assert service_for(root).first_request_day() == date(2026, 10, 5)


def test_first_request_day_is_none_for_an_empty_store(root, service_for):
    assert service_for(root).first_request_day() is None
