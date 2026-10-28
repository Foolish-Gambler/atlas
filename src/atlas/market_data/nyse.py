"""NYSE RTH calendar for 2024–2025. Stdlib only; not a live exchange feed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

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
        out: list[NyseSession] = []
        cursor = start
        while cursor <= end:
            session = self.session_on(cursor)
            if session is not None:
                out.append(session)
            cursor += timedelta(days=1)
        return tuple(out)
