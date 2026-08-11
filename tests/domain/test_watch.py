"""Domain watch type tests."""

from datetime import date, time, timedelta

import pytest

from cinema_friend.domain.errors import InputError
from cinema_friend.domain.state import WatchMode
from cinema_friend.domain.watch import WatchCriteria


def test_recurring_criteria_require_interval():
    with pytest.raises(InputError, match="interval"):
        WatchCriteria(
            source_url="https://whatson.bfi.org.uk/imax/Online/article/dog-stars",
            slug="dog-stars",
            date_from=date(2026, 8, 26),
            date_to=date(2026, 8, 30),
            time_from=time(18, 0),
            time_to=time(23, 0),
            quantity=2,
            mode=WatchMode.RECURRING,
            interval=None,
        )


def test_one_off_criteria_reject_interval():
    with pytest.raises(InputError, match="interval"):
        WatchCriteria(
            source_url="https://whatson.bfi.org.uk/Online/article/dog-stars",
            slug="dog-stars",
            date_from=date(2026, 8, 26),
            date_to=date(2026, 8, 26),
            time_from=time(18, 0),
            time_to=time(23, 0),
            quantity=1,
            mode=WatchMode.ONE_OFF,
            interval=timedelta(minutes=30),
        )


def test_quantity_out_of_range():
    with pytest.raises(InputError, match="quantity"):
        WatchCriteria(
            source_url="https://whatson.bfi.org.uk/Online/article/dog-stars",
            slug="dog-stars",
            date_from=date(2026, 8, 26),
            date_to=date(2026, 8, 26),
            time_from=time(18, 0),
            time_to=time(23, 0),
            quantity=9,
            mode=WatchMode.ONE_OFF,
        )
