"""Tests for the shared daily-window predicate.

One predicate answers "is this local clock time inside the watch's daily window" for
both :class:`~cinema_friend.domain.watch.WatchCriteria` construction and performance
filtering, so a window can never be accepted by one and silently ignored by the other.
"""

from __future__ import annotations

from datetime import time

from cinema_friend.domain.time_window import within_daily_window


def test_plain_window_includes_both_endpoints() -> None:
    assert within_daily_window(time(18, 0), time(23, 0), time(18, 0))
    assert within_daily_window(time(18, 0), time(23, 0), time(20, 30))
    assert within_daily_window(time(18, 0), time(23, 0), time(23, 0))


def test_plain_window_excludes_times_outside_it() -> None:
    assert not within_daily_window(time(18, 0), time(23, 0), time(17, 59))
    assert not within_daily_window(time(18, 0), time(23, 0), time(23, 1))


def test_wrapping_window_accepts_both_halves_and_both_endpoints() -> None:
    """``22:00 to 01:00`` is a window that crosses midnight, not a reversed one."""
    for value in (time(22, 0), time(23, 30), time(0, 30), time(1, 0)):
        assert within_daily_window(time(22, 0), time(1, 0), value), value


def test_wrapping_window_rejects_the_daytime_gap() -> None:
    for value in (time(1, 1), time(14, 0), time(21, 59)):
        assert not within_daily_window(time(22, 0), time(1, 0), value), value


def test_window_of_a_single_instant_matches_only_that_instant() -> None:
    assert within_daily_window(time(20, 0), time(20, 0), time(20, 0))
    assert not within_daily_window(time(20, 0), time(20, 0), time(20, 1))


def test_midnight_to_midnight_matches_only_midnight() -> None:
    assert within_daily_window(time(0, 0), time(0, 0), time(0, 0))
    assert not within_daily_window(time(0, 0), time(0, 0), time(12, 0))
