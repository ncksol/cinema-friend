"""Wall-clock helpers shared by criteria construction and performance filtering.

Everything the user says about times is said in the cinema's own timezone, so the
London zone and the daily-window predicate live here rather than being restated by
each layer. A second copy of :func:`within_daily_window` is a correctness bug waiting
to happen: the two copies drift, and a window such as ``22:00`` to ``01:00`` is then
accepted at construction and silently never matched, or the reverse.
"""

from __future__ import annotations

from datetime import date, time
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
DailyTimeWindow = tuple[time, time]


def within_daily_window(time_from: time, time_to: time, value: time) -> bool:
    """Is ``value`` inside the daily window ``time_from``..``time_to``, inclusive?

    A window whose end is not after its start wraps past midnight, so ``22:00`` to
    ``01:00`` covers the late evening and the small hours but not the day between.
    """
    if time_from <= time_to:
        return time_from <= value <= time_to
    return value >= time_from or value <= time_to


def window_for_local_date(
    local_date: date,
    *,
    default_window: DailyTimeWindow,
    weekend_window: DailyTimeWindow | None,
) -> DailyTimeWindow:
    if local_date.weekday() >= 5 and weekend_window is not None:
        return weekend_window
    return default_window
