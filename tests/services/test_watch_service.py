"""Tests for cinema_friend.services.watch_service.

A ``WatchService`` call is one unit of work: it owns the connection and the transaction
around every repository call it makes, and it never reveals one user's watch to another
-- a watch that exists but belongs to someone else looks exactly like a watch that does
not exist at all.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from cinema_friend.domain.errors import InputError
from cinema_friend.domain.state import WatchMode, WatchStatus
from cinema_friend.domain.watch import WatchCriteria
from cinema_friend.services.watch_service import WatchService
from cinema_friend.storage.database import Database
from cinema_friend.storage.watch_repository import WatchRepository
from tests.fakes import FakeClock

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def criteria(**overrides: object) -> WatchCriteria:
    defaults: dict[str, object] = {
        "source_url": "https://whatson.bfi.org.uk/imax/Online/article/dog-stars",
        "slug": "dog-stars",
        "date_from": date(2026, 8, 26),
        "date_to": date(2026, 8, 30),
        "time_from": time(18, 0),
        "time_to": time(23, 0),
        "quantity": 2,
        "mode": WatchMode.ONE_OFF,
    }
    defaults.update(overrides)
    return WatchCriteria(**defaults)  # type: ignore[arg-type]


def recurring_criteria(**overrides: object) -> WatchCriteria:
    overrides.setdefault("interval", timedelta(minutes=30))
    return criteria(mode=WatchMode.RECURRING, **overrides)


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "cinema.db")
    async with instance.connection() as connection:
        await instance.migrate(connection)
    return instance


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def service(database: Database, fake_clock: FakeClock) -> WatchService:
    return WatchService(database, WatchRepository(), fake_clock)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


async def test_create_persists_an_active_watch_with_an_immediate_check(
    service: WatchService, fake_clock: FakeClock
) -> None:
    watch = await service.create(11, criteria())

    assert watch.status is WatchStatus.ACTIVE
    assert watch.next_check_at == fake_clock.now()
    assert watch.title is None
    assert watch.last_check_at is None
    assert await service.get_owned(11, watch.watch_id) == watch


async def test_create_canonicalizes_the_source_url_and_slug(service: WatchService) -> None:
    """A caller-supplied slug or URL shape must not silently disagree with the article."""
    watch = await service.create(
        22,
        criteria(
            source_url=(
                "https://whatson.bfi.org.uk/imax/Online/default.asp?"
                "BOparam::WScontent::loadArticle::permalink=dog-stars"
            ),
            slug="stale-slug",
        ),
    )

    assert watch.criteria.slug == "dog-stars"
    assert watch.criteria.source_url == (
        "https://whatson.bfi.org.uk/imax/Online/default.asp?"
        "BOparam::WScontent::loadArticle::permalink=dog-stars"
    )


async def test_create_rejects_a_non_bfi_source_url(service: WatchService) -> None:
    with pytest.raises(InputError):
        await service.create(11, criteria(source_url="https://example.com/dog-stars"))


# ---------------------------------------------------------------------------
# Listing and ownership-scoped reads
# ---------------------------------------------------------------------------


async def test_list_for_owner_excludes_other_owners_watches(service: WatchService) -> None:
    mine = await service.create(11, criteria())
    await service.create(22, criteria(slug="other-film", source_url=(
        "https://whatson.bfi.org.uk/imax/Online/article/other-film"
    )))

    listed = await service.list_for_owner(11)

    assert [watch.watch_id for watch in listed] == [mine.watch_id]


async def test_get_owned_rejects_a_different_owner(service: WatchService) -> None:
    watch = await service.create(11, criteria())

    with pytest.raises(InputError, match="watch not found"):
        await service.get_owned(22, watch.watch_id)


async def test_get_owned_rejects_an_unknown_watch(service: WatchService) -> None:
    from uuid import uuid4

    with pytest.raises(InputError, match="watch not found"):
        await service.get_owned(11, uuid4())


# ---------------------------------------------------------------------------
# Pause / resume ownership and scheduling
# ---------------------------------------------------------------------------


async def test_pause_rejects_a_different_owner(service: WatchService) -> None:
    watch = await service.create(11, criteria())

    with pytest.raises(InputError, match="watch not found"):
        await service.pause(owner_user_id=22, watch_id=watch.watch_id)


async def test_pause_clears_the_next_check(service: WatchService) -> None:
    watch = await service.create(11, criteria())

    paused = await service.pause(owner_user_id=11, watch_id=watch.watch_id)

    assert paused.status is WatchStatus.PAUSED
    assert paused.next_check_at is None
    assert await service.get_owned(11, watch.watch_id) == paused


async def test_resume_rejects_a_different_owner(service: WatchService) -> None:
    watch = await service.create(11, recurring_criteria())
    await service.pause(11, watch.watch_id)

    with pytest.raises(InputError, match="watch not found"):
        await service.resume(owner_user_id=22, watch_id=watch.watch_id)


async def test_resume_schedules_an_immediate_check(
    service: WatchService, fake_clock: FakeClock
) -> None:
    watch = await service.create(11, recurring_criteria())
    await service.pause(11, watch.watch_id)

    resumed = await service.resume(11, watch.watch_id)

    assert resumed.status is WatchStatus.ACTIVE
    assert resumed.next_check_at == fake_clock.now()


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def test_delete_rejects_a_different_owner(service: WatchService) -> None:
    watch = await service.create(11, criteria())

    with pytest.raises(InputError, match="watch not found"):
        await service.delete(owner_user_id=22, watch_id=watch.watch_id)

    assert await service.get_owned(11, watch.watch_id) == watch


async def test_delete_removes_the_watch(service: WatchService) -> None:
    watch = await service.create(11, criteria())

    await service.delete(owner_user_id=11, watch_id=watch.watch_id)

    with pytest.raises(InputError, match="watch not found"):
        await service.get_owned(11, watch.watch_id)
