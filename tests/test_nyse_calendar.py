from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from atlas.market_data.nyse import EASTERN, NyseCalendar, UnsupportedCalendarError


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


def test_unsupported_year_is_rejected() -> None:
    calendar = NyseCalendar()
    with pytest.raises(UnsupportedCalendarError, match="2026"):
        calendar.session_on(date(2026, 1, 5))
    with pytest.raises(UnsupportedCalendarError, match="2023"):
        calendar.sessions(date(2023, 1, 3), date(2023, 1, 10))


def test_official_2024_2025_holidays_and_early_closes() -> None:
    calendar = NyseCalendar()
    holidays = {
        date(2024, 1, 1),
        date(2024, 1, 15),
        date(2024, 2, 19),
        date(2024, 3, 29),
        date(2024, 5, 27),
        date(2024, 6, 19),
        date(2024, 7, 4),
        date(2024, 9, 2),
        date(2024, 11, 28),
        date(2024, 12, 25),
        date(2025, 1, 1),
        date(2025, 1, 9),
        date(2025, 1, 20),
        date(2025, 2, 17),
        date(2025, 4, 18),
        date(2025, 5, 26),
        date(2025, 6, 19),
        date(2025, 7, 4),
        date(2025, 9, 1),
        date(2025, 11, 27),
        date(2025, 12, 25),
    }
    early = {
        date(2024, 7, 3),
        date(2024, 11, 29),
        date(2024, 12, 24),
        date(2025, 7, 3),
        date(2025, 11, 28),
        date(2025, 12, 24),
    }
    for day in holidays:
        assert calendar.session_on(day) is None, day
    for day in early:
        session = calendar.session_on(day)
        assert session is not None, day
        assert session.early_close is True
        assert session.close.hour == 13


def test_january_9_2025_national_day_of_mourning() -> None:
    calendar = NyseCalendar()
    assert calendar.is_holiday(date(2025, 1, 9))
    assert calendar.session_on(date(2025, 1, 9)) is None
    wednesday = calendar.session_on(date(2025, 1, 8))
    friday = calendar.session_on(date(2025, 1, 10))
    assert wednesday is not None and friday is not None
    week = calendar.sessions(date(2025, 1, 6), date(2025, 1, 10))
    assert [session.session_date for session in week] == [
        date(2025, 1, 6),
        date(2025, 1, 7),
        date(2025, 1, 8),
        date(2025, 1, 10),
    ]
