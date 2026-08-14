"""Watch-matching, seat-block, and ranking algorithms."""

from __future__ import annotations

from datetime import date

from cinema_friend.domain.bfi import Performance
from cinema_friend.domain.time_window import within_daily_window
from cinema_friend.domain.watch import WatchCriteria

_ON_SALE_BASE_CODES = frozenset({"S", "O", "R"})


def is_on_sale(performance: Performance) -> bool:
    """Return True if *performance*'s sales status means BFI is currently selling it.

    The trailing ``*`` some codes carry is a display marker, not a different status, so
    the comparison is against :attr:`~Performance.sales_status_base`. Shared with the
    smoke command so there is exactly one definition of "on sale" to drift.
    """
    return performance.sales_status_base in _ON_SALE_BASE_CODES


def _matches_date_window(criteria: WatchCriteria, local_date: date) -> bool:
    return criteria.date_from <= local_date <= criteria.date_to


def performance_matches(criteria: WatchCriteria, performance: Performance) -> bool:
    """Return True if *performance* satisfies *criteria*'s eligibility filters.

    Checks date/time window (using the performance's Europe/London local
    time, with support for a window that wraps past midnight), the on-sale
    base sales-status code, reserved seating, and sufficient availability for
    the requested quantity.
    """
    if not is_on_sale(performance):
        return False
    if not performance.reserved_seating:
        return False
    if performance.availability_num < criteria.quantity:
        return False
    local_start = performance.start
    time_from, time_to = criteria.time_window_for(local_start.date())
    return _matches_date_window(criteria, local_start.date()) and within_daily_window(
        time_from,
        time_to,
        local_start.time(),
    )
