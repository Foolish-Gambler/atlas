from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from atlas.market_data.nyse import EASTERN, NyseCalendar


def test_holiday_weekend_and_early_close() -> None:
    calendar = NyseCalendar()
    assert calendar.session_on(date(2024, 1, 1)) is None
    assert calendar.session_on(date(2024, 1, 6)) is None
    assert calendar.session_on(date(2024, 1, 7)) is None
    regular = calendar.session_on(date(2024, 1, 2))
    assert regular is not None
    assert regular.early_close is False
    assert regular.open.hour == 9 and regular.open.minute == 30
    assert regular.close.hour == 16
    early = calendar.session_on(date(2024, 11, 29))
    assert early is not None
    assert early.early_close is True
    assert early.close.hour == 13


def test_dst_spring_forward_offsets() -> None:
    calendar = NyseCalendar()
    friday = calendar.session_on(date(2024, 3, 8))
    monday = calendar.session_on(date(2024, 3, 11))
    assert friday is not None and monday is not None
    assert friday.open.utcoffset() == timedelta(hours=-5)
    assert monday.open.utcoffset() == timedelta(hours=-4)
    assert friday.open.astimezone(UTC) == datetime(2024, 3, 8, 14, 30, tzinfo=UTC)
    assert monday.open.astimezone(UTC) == datetime(2024, 3, 11, 13, 30, tzinfo=UTC)
    assert friday.open.tzinfo == EASTERN
