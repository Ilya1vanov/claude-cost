"""Day arithmetic in one place. Reporting days are local days in the configured time zone
(`tz`; None = the system's local time); price effective dates are UTC days (see price_day)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo


def local_day(epoch: float, tz: tzinfo | None) -> date:
    """The local calendar day an epoch timestamp falls on."""
    return datetime.fromtimestamp(epoch, tz).date()


def today(now: datetime, tz: tzinfo | None) -> date:
    """`now` as a local calendar day."""
    return now.astimezone(tz).date() if tz else now.date()


def day_start(day: date, tz: tzinfo | None) -> float:
    """Epoch of local midnight at the start of `day`."""
    return datetime.combine(day, time.min, tz).timestamp()


def day_end(day: date, tz: tzinfo | None) -> float:
    """Epoch of local midnight ending `day` (exclusive upper bound)."""
    return day_start(day + timedelta(1), tz)


def price_day(epoch: float) -> date:
    """The UTC day a request is priced on. Deliberately not the local day: the price table's
    effective dates and the stored costs are UTC-based, so ingest and the Compact analysis
    must agree on it regardless of the configured time zone."""
    return datetime.fromtimestamp(epoch, timezone.utc).date()


def parse_since(text: str, today_: date) -> date:
    """A `since` value: an ISO date (2026-10-01) or a number of days back (7d).
    Raises ValueError when it is neither."""
    text = text.strip()
    if text.endswith("d"):
        return today_ - timedelta(days=int(text[:-1]))
    return date.fromisoformat(text)
