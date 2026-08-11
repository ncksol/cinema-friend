"""Watch-matching, seat-block, and ranking algorithms."""

from __future__ import annotations

from datetime import date, time

from cinema_friend.domain.bfi import Performance
from cinema_friend.domain.watch import WatchCriteria

_ON_SALE_BASE_CODES = frozenset({"S", "O", "R"})


def _matches_date_window(criteria: WatchCriteria, local_date: date) -> bool:
    return criteria.date_from <= local_date <= criteria.date_to


def _matches_time_window(criteria: WatchCriteria, local_time: time) -> bool:
    if criteria.time_from <= criteria.time_to:
        return criteria.time_from <= local_time <= criteria.time_to
    # The window wraps past midnight (e.g. 22:00 to 01:00): a time matches if
    # it falls in either the late-evening or early-morning half of the window.
    return local_time >= criteria.time_from or local_time <= criteria.time_to


def performance_matches(criteria: WatchCriteria, performance: Performance) -> bool:
    """Return True if *performance* satisfies *criteria*'s eligibility filters.

    Checks date/time window (using the performance's Europe/London local
    time, with support for a window that wraps past midnight), the on-sale
    base sales-status code, reserved seating, and sufficient availability for
    the requested quantity.
    """
    if performance.sales_status_base not in _ON_SALE_BASE_CODES:
        return False
    if not performance.reserved_seating:
        return False
    if performance.availability_num < criteria.quantity:
        return False
    local_start = performance.start
    return _matches_date_window(criteria, local_start.date()) and _matches_time_window(
        criteria, local_start.time()
    )
