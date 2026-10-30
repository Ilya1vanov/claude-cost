"""Seam 1: Comparison of two or more same-mode periods through the query service."""
from datetime import date

import pytest

from conftest import NOW
from helpers import req


def _spend(root, session, day, mid, output=1_000_000):
    root.write_transcript("p", session, [req(mid, f"{day}T10:00:00Z", output=output)])


def test_non_adjacent_weeks_overlaid_by_day_of_period(root, service_for):
    _spend(root, "a", "2026-10-26", "a1")                    # week of 26th, day 1: $20
    _spend(root, "b", "2026-10-13", "b1", output=2_000_000)  # week of 12th, day 2: $40
    _spend(root, "c", "2026-09-28", "c1", output=3_000_000)  # week of 28 Sep, day 1: $60
    _spend(root, "d", "2026-10-05", "d1")                    # week of 5th: not selected
    c = service_for(root).comparison(
        "week", [date(2026, 10, 26), date(2026, 9, 28), date(2026, 10, 12)], NOW)
    assert [e.period.start for e in c.entries] == [
        date(2026, 9, 28), date(2026, 10, 12), date(2026, 10, 26)]
    assert c.baseline == date(2026, 9, 28)            # defaults to the earliest
    assert [e.cost for e in c.entries] == [pytest.approx(60), pytest.approx(40), pytest.approx(20)]
    assert c.entries[0].daily[0] == pytest.approx(60)  # day 1 of the first period
    assert c.entries[1].daily[:2] == [0, pytest.approx(40)]
    assert len(c.entries[1].daily) == 7
    assert c.entries[2].period.end == date(2026, 11, 1)
    assert len(c.entries[2].daily) == 5               # current week is partial: through today
    assert c.entries[2].partial and not c.entries[0].partial
    assert c.day_count == 7


def test_deltas_are_against_the_chosen_baseline(root, service_for):
    _spend(root, "a", "2026-10-26", "a1", output=1_000_000)   # $20
    _spend(root, "b", "2026-10-12", "b1", output=3_000_000)   # $60
    _spend(root, "c", "2026-09-28", "c1", output=2_000_000)   # $40
    svc = service_for(root)
    starts = [date(2026, 9, 28), date(2026, 10, 12), date(2026, 10, 26)]
    default = svc.comparison("week", starts, NOW)
    by = {e.period.start: e for e in default.entries}
    assert by[date(2026, 9, 28)].delta_cost == 0 and by[date(2026, 9, 28)].delta_pct == 0
    assert by[date(2026, 10, 12)].delta_cost == pytest.approx(20)
    assert by[date(2026, 10, 12)].delta_pct == pytest.approx(50)
    assert by[date(2026, 10, 26)].delta_pct == pytest.approx(-50)
    chosen = svc.comparison("week", starts, NOW, baseline=date(2026, 10, 12))
    assert chosen.baseline == date(2026, 10, 12)
    by = {e.period.start: e for e in chosen.entries}
    assert by[date(2026, 9, 28)].delta_cost == pytest.approx(-20)
    assert by[date(2026, 9, 28)].delta_pct == pytest.approx(-100 / 3)
    assert by[date(2026, 10, 26)].delta_cost == pytest.approx(-40)


def test_percent_is_none_when_baseline_cost_is_zero(root, service_for):
    _spend(root, "a", "2026-10-26", "a1")
    c = service_for(root).comparison("week", [date(2026, 10, 12), date(2026, 10, 26)], NOW)
    assert c.entries[1].delta_cost == pytest.approx(20) and c.entries[1].delta_pct is None


def test_months_have_their_own_lengths(root, service_for):
    _spend(root, "a", "2026-09-30", "a1")   # day 30 of September
    _spend(root, "b", "2026-02-01", "b1")   # day 1 of February
    c = service_for(root).comparison("month", [date(2026, 2, 1), date(2026, 9, 1)], NOW)
    assert [len(e.daily) for e in c.entries] == [28, 30]
    assert c.entries[1].daily[29] == pytest.approx(20) and c.day_count == 30


@pytest.mark.parametrize("mode,starts", [
    ("week", [date(2026, 10, 26)]),                           # fewer than two
    ("week", [date(2026, 10, 26), date(2026, 10, 26)]),       # duplicates count once
    ("week", [date(2026, 10, 26), date(2026, 10, 14)]),       # not a Monday
    ("month", [date(2026, 9, 1), date(2026, 10, 2)]),         # not the 1st
    ("last7", [date(2026, 10, 1), date(2026, 10, 8)]),        # rolling periods can't be compared
    ("week", [date(2026, 10, 26), date(2026, 11, 2)]),        # not started yet
])
def test_invalid_selections_are_rejected(root, service_for, mode, starts):
    with pytest.raises(ValueError):
        service_for(root).comparison(mode, starts, NOW)


def test_baseline_must_be_one_of_the_selected_periods(root, service_for):
    with pytest.raises(ValueError):
        service_for(root).comparison("week", [date(2026, 10, 12), date(2026, 10, 26)], NOW,
                                     baseline=date(2026, 10, 5))


def test_available_periods_run_from_first_request_to_now_newest_first(root, service_for):
    _spend(root, "a", "2026-09-30", "a1")   # Wednesday; week of 28 Sep
    svc = service_for(root)
    weeks = svc.available_periods("week", NOW)
    assert weeks[0] == date(2026, 10, 26) and weeks[-1] == date(2026, 9, 28) and len(weeks) == 5
    assert svc.available_periods("month", NOW) == [date(2026, 10, 1), date(2026, 9, 1)]


def test_available_periods_empty_store(service_for, root):
    assert service_for(root).available_periods("week", NOW) == []
