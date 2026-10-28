"""NYSE RTH calendar for 2024–2025. Stdlib only; not a live exchange feed.

Holiday and early-close dates follow NYSE Group notices:
https://ir.theice.com/press/news-details/2023/NYSE-Group-Announces-2024-2025-and-2026-Holiday-and-Early-Closings-Calendar/default.aspx
https://ir.theice.com/press/news-details/2024/The-New-York-Stock-Exchange-Will-Close-Markets-on-January-9-to-Honor-the-Passing-of-Former-President-Jimmy-Carter-on-National-Day-of-Mourning/default.aspx
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
SUPPORTED_YEARS = frozenset({2024, 2025})


class UnsupportedCalendarError(ValueError):
    """Holiday/early-close tables are maintained only for listed years."""


# Full-day closures. January 9, 2025 is a National Day of Mourning (Jimmy Carter),
# not a recurring holiday; omit it and that Thursday is incorrectly tradable.
_HOLIDAYS = frozenset(
    {
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
)

_EARLY_CLOSE = frozenset(
    {
        date(2024, 7, 3),
        date(2024, 11, 29),
        date(2024, 12, 24),
        date(2025, 7, 3),
        date(2025, 11, 28),
        date(2025, 12, 24),
    }
)

_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)
_EARLY_CLOSE_TIME = time(13, 0)


@dataclass(frozen=True, slots=True)
class NyseSession:
    session_date: date
    open: datetime
    close: datetime
    early_close: bool

    def contains(self, instant: datetime) -> bool:
        local = instant.astimezone(EASTERN)
        return self.open <= local < self.close


class NyseCalendar:
    """Regular-hours sessions, holidays, early closes, and DST via America/New_York."""

    def is_holiday(self, day: date) -> bool:
        return day in _HOLIDAYS

    def is_weekend(self, day: date) -> bool:
        return day.weekday() >= 5

    def session_on(self, day: date) -> NyseSession | None:
        _require_supported_year(day)
        if self.is_weekend(day) or self.is_holiday(day):
            return None
        early = day in _EARLY_CLOSE
        close_clock = _EARLY_CLOSE_TIME if early else _RTH_CLOSE
        return NyseSession(
            session_date=day,
            open=datetime.combine(day, _RTH_OPEN, tzinfo=EASTERN),
            close=datetime.combine(day, close_clock, tzinfo=EASTERN),
            early_close=early,
        )

    def sessions(self, start: date, end: date) -> tuple[NyseSession, ...]:
        if end < start:
            raise ValueError("end cannot precede start.")
        _require_supported_year(start)
        _require_supported_year(end)
        out: list[NyseSession] = []
        cursor = start
        while cursor <= end:
            session = self.session_on(cursor)
            if session is not None:
                out.append(session)
            cursor += timedelta(days=1)
        return tuple(out)


def _require_supported_year(day: date) -> None:
    if day.year not in SUPPORTED_YEARS:
        supported = ", ".join(str(year) for year in sorted(SUPPORTED_YEARS))
        raise UnsupportedCalendarError(
            f"NYSE calendar has no maintained holidays/early closes for {day.year}; "
            f"supported years: {supported}."
        )
