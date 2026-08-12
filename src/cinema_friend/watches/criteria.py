"""Watch-matching, seat-block, and ranking algorithms."""

from __future__ import annotations

from datetime import date

from cinema_friend.domain.bfi import Performance
from cinema_friend.domain.time_window import within_daily_window
from cinema_friend.domain.watch import WatchCriteria

_ON_SALE_BASE_CODES = frozenset({"S", "O", "R"})


def _matches_date_window(criteria: WatchCriteria, local_date: date) -> bool:
    return criteria.date_from <= local_date <= criteria.date_to


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
    return _matches_date_window(criteria, local_start.date()) and within_daily_window(
        criteria.time_from, criteria.time_to, local_start.time()
    )
