"""Domain watch type tests."""

from datetime import UTC, date, datetime, time, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from cinema_friend.domain.errors import InputError
from cinema_friend.domain.state import WatchMode, WatchStatus
from cinema_friend.domain.watch import Watch, WatchCriteria


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


_BASE = {
    "source_url": "https://whatson.bfi.org.uk/Online/article/dog-stars",
    "slug": "dog-stars",
    "date_from": date(2026, 8, 26),
    "date_to": date(2026, 8, 30),
    "time_from": time(18, 0),
    "time_to": time(23, 0),
    "quantity": 2,
    "mode": WatchMode.ONE_OFF,
}


def test_preferred_utc_instant_rejects_naive():
    with pytest.raises(InputError, match="UTC"):
        WatchCriteria(**_BASE, preferred_utc_instant=datetime(2026, 8, 27, 19, 0))  # noqa: DTZ001


def test_preferred_utc_instant_rejects_non_utc():
    paris = timezone(timedelta(hours=2))
    with pytest.raises(InputError, match="UTC"):
        WatchCriteria(**_BASE, preferred_utc_instant=datetime(2026, 8, 27, 19, 0, tzinfo=paris))


def test_preferred_instant_is_validated_against_its_london_local_time_in_bst():
    """In August the instant's UTC clock reading is an hour behind the cinema's.

    ``17:30Z`` is ``18:30`` in London and belongs inside an 18:00-23:00 window;
    ``22:30Z`` is ``23:30`` in London and does not. Reading the UTC components
    directly gets both backwards.
    """
    WatchCriteria(**_BASE, preferred_utc_instant=datetime(2026, 8, 27, 17, 30, tzinfo=UTC))
    with pytest.raises(InputError, match="time"):
        WatchCriteria(**_BASE, preferred_utc_instant=datetime(2026, 8, 27, 22, 30, tzinfo=UTC))


def test_preferred_instant_is_validated_against_its_london_local_time_in_gmt():
    """In January London is UTC, so the same reading validates unshifted."""
    winter = {**_BASE, "date_from": date(2026, 1, 10), "date_to": date(2026, 1, 12)}
    WatchCriteria(**winter, preferred_utc_instant=datetime(2026, 1, 10, 18, 30, tzinfo=UTC))
    with pytest.raises(InputError, match="time"):
        WatchCriteria(**winter, preferred_utc_instant=datetime(2026, 1, 10, 17, 30, tzinfo=UTC))


def test_preferred_instant_date_range_uses_the_london_calendar_date():
    """``2026-08-26T23:30Z`` already belongs to the 27th in London."""
    window = {
        **_BASE,
        "time_from": time(22, 0),
        "time_to": time(1, 0),
        "date_from": date(2026, 8, 26),
        "date_to": date(2026, 8, 27),
    }
    WatchCriteria(**window, preferred_utc_instant=datetime(2026, 8, 26, 23, 30, tzinfo=UTC))
    with pytest.raises(InputError, match="date"):
        WatchCriteria(
            **{**window, "date_to": date(2026, 8, 26)},
            preferred_utc_instant=datetime(2026, 8, 26, 23, 30, tzinfo=UTC),
        )


def test_preferred_instant_accepts_a_window_that_crosses_midnight():
    """A wrapping window must not make every preferred instant unrepresentable."""
    window = {
        **_BASE,
        "time_from": time(22, 0),
        "time_to": time(1, 0),
        "date_from": date(2026, 8, 26),
        "date_to": date(2026, 8, 28),
    }
    # 23:30 and 00:30 London, both inside 22:00-01:00.
    WatchCriteria(**window, preferred_utc_instant=datetime(2026, 8, 26, 22, 30, tzinfo=UTC))
    WatchCriteria(**window, preferred_utc_instant=datetime(2026, 8, 26, 23, 30, tzinfo=UTC))
    # 14:00 London falls in the window's daytime gap.
    with pytest.raises(InputError, match="time"):
        WatchCriteria(**window, preferred_utc_instant=datetime(2026, 8, 26, 13, 0, tzinfo=UTC))


def test_watch_identity_is_a_uuid():
    """Watch IDs are client-generated UUIDs so a watch can be referenced before insert."""
    watch_id = uuid4()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    watch = Watch(
        watch_id=watch_id,
        user_id=11,
        criteria=WatchCriteria(**_BASE),
        status=WatchStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    assert isinstance(watch.watch_id, UUID)
    assert watch.watch_id == watch_id


def test_watch_title_and_last_check_at_are_optional():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    defaults = Watch(
        watch_id=uuid4(),
        user_id=11,
        criteria=WatchCriteria(**_BASE),
        status=WatchStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    assert defaults.title is None
    assert defaults.last_check_at is None

    populated = Watch(
        watch_id=uuid4(),
        user_id=11,
        criteria=WatchCriteria(**_BASE),
        status=WatchStatus.ACTIVE,
        created_at=now,
        updated_at=now,
        title="Dog Stars",
        last_check_at=now,
    )
    assert populated.title == "Dog Stars"
    assert populated.last_check_at == now


def test_watch_status_covers_every_lifecycle_state():
    assert {status.value for status in WatchStatus} == {
        "active",
        "paused",
        "backoff",
        "completed",
        "expired",
        "failed",
    }
