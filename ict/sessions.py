"""One session clock for both books.

The desk used to carry two hardcoded UTC-hour tables — one in ict/model.py,
one in fabio/model.py — that disagreed about when "NY" is, while ict/model.py
anchored its daily/weekly structure to New York local time. A killzone is a
local market hour, so the windows here live in each market's own timezone and
follow DST instead of sliding an hour twice a year.

Each window is set so standard-time (winter) behaviour matches the old UTC
tables exactly. Only the summer half moves, which is the half that was wrong.

ICT trades the AM killzone. Fabio needs the whole cash session to build a
value area. Those are different windows on purpose — but one clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
TOKYO = ZoneInfo("Asia/Tokyo")

DEAD_SCORE = 2


@dataclass(frozen=True)
class Window:
    """A session in its own local hours. end_hour is exclusive."""

    name: str
    score: int
    tz: ZoneInfo
    start_hour: int
    end_hour: int


@dataclass(frozen=True)
class Session:
    name: str
    score: int
    start: datetime | None = None
    end: datetime | None = None

    @property
    def live(self) -> bool:
        return self.name != "dead"


# First window containing `now` wins, so NY beats an overlapping London tail.
ICT_KILLZONES = (
    Window("ny", 5, NY, 8, 11),
    Window("london", 4, LONDON, 7, 11),
    Window("asia", 3, TOKYO, 9, 17),
)

# Fabio fades the session value area, so it needs the full cash session.
FABIO_SESSIONS = (
    Window("ny", 5, NY, 8, 16),
    Window("london", 4, LONDON, 7, 13),
    Window("asia", 3, TOKYO, 9, 17),
)

DEAD = Session("dead", DEAD_SCORE)


def window_bounds(window: Window, now: datetime) -> tuple[datetime, datetime]:
    """Today's window in UTC, for the local day `now` falls on."""
    local = now.astimezone(window.tz)
    start = local.replace(hour=window.start_hour, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=window.end_hour - window.start_hour)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def current(now: datetime | None = None, windows: tuple[Window, ...] = ICT_KILLZONES) -> Session:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    for window in windows:
        start, end = window_bounds(window, now)
        if start <= now < end:
            return Session(window.name, window.score, start, end)
    return DEAD


def session_score(
    now: datetime | None = None,
    windows: tuple[Window, ...] = ICT_KILLZONES,
) -> tuple[int, str]:
    session = current(now, windows)
    return session.score, session.name
