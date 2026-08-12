"""Tests for cinema_friend.telegram.auth.

Every denial path here must be indistinguishable from every other: a missing user, a
user outside the allow list, a watch that does not exist, and a watch owned by someone
else all raise the same :class:`InputError`, so a caller can never use the error to
enumerate valid user or watch IDs.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path
from uuid import uuid4

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User

from cinema_friend.domain.errors import InputError
from cinema_friend.domain.state import WatchMode
from cinema_friend.domain.watch import WatchCriteria
from cinema_friend.services.watch_service import WatchService
from cinema_friend.storage.database import Database
from cinema_friend.storage.watch_repository import WatchRepository
from cinema_friend.telegram.auth import authorized_user_id, require_owned_watch
from tests.fakes import FakeClock

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_ALLOWED = frozenset({11, 22})


def _criteria() -> WatchCriteria:
    return WatchCriteria(
        source_url="https://whatson.bfi.org.uk/imax/Online/article/dog-stars",
        slug="dog-stars",
        date_from=date(2026, 8, 26),
        date_to=date(2026, 8, 30),
        time_from=time(18, 0),
        time_to=time(23, 0),
        quantity=2,
        mode=WatchMode.ONE_OFF,
    )


def _update_with_user(user_id: int | None) -> Update:
    if user_id is None:
        return Update(update_id=1)
    user = User(id=user_id, first_name="Test", is_bot=False)
    chat = Chat(id=user_id, type="private")
    message = Message(message_id=1, date=NOW, chat=chat, from_user=user)
    return Update(update_id=1, message=message)


def _update_with_callback_user(user_id: int) -> Update:
    """Build an ``Update`` carrying its user on ``callback_query`` instead of ``message``.

    ``effective_user`` must find the user regardless of which sub-object carries it,
    since a paging or watch-action callback arrives this way, not as a message.
    """
    user = User(id=user_id, first_name="Test", is_bot=False)
    callback_query = CallbackQuery(
        id="1", from_user=user, chat_instance="chat-instance", data="v1:w:pause:x"
    )
    return Update(update_id=1, callback_query=callback_query)


# ---------------------------------------------------------------------------
# authorized_user_id
# ---------------------------------------------------------------------------


def test_authorized_user_id_returns_an_allow_listed_user() -> None:
    update = _update_with_user(11)

    assert authorized_user_id(update, _ALLOWED) == 11


def test_authorized_user_id_finds_the_user_on_a_callback_query() -> None:
    update = _update_with_callback_user(22)

    assert authorized_user_id(update, _ALLOWED) == 22


def test_authorized_user_id_denies_a_user_outside_the_allow_list() -> None:
    update = _update_with_user(99)

    with pytest.raises(InputError):
        authorized_user_id(update, _ALLOWED)


def test_authorized_user_id_denies_an_update_with_no_user() -> None:
    update = _update_with_user(None)

    with pytest.raises(InputError):
        authorized_user_id(update, _ALLOWED)


def test_authorized_user_id_denial_is_identical_for_missing_and_disallowed_users() -> None:
    missing_error: str | None = None
    disallowed_error: str | None = None
    try:
        authorized_user_id(_update_with_user(None), _ALLOWED)
    except InputError as exc:
        missing_error = str(exc)
    try:
        authorized_user_id(_update_with_user(99), _ALLOWED)
    except InputError as exc:
        disallowed_error = str(exc)

    assert missing_error is not None
    assert missing_error == disallowed_error


# ---------------------------------------------------------------------------
# require_owned_watch
# ---------------------------------------------------------------------------


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "cinema.db")
    async with instance.connection() as connection:
        await instance.migrate(connection)
    return instance


@pytest.fixture
def watch_service(database: Database) -> WatchService:
    return WatchService(database, WatchRepository(), FakeClock(NOW))


async def test_require_owned_watch_returns_the_owners_watch(watch_service: WatchService) -> None:
    watch = await watch_service.create(11, _criteria())
    update = _update_with_user(11)

    result = await require_owned_watch(update, watch_service, watch.watch_id)

    assert result == watch


async def test_require_owned_watch_denies_a_different_owner(watch_service: WatchService) -> None:
    watch = await watch_service.create(11, _criteria())
    update = _update_with_user(22)

    with pytest.raises(InputError):
        await require_owned_watch(update, watch_service, watch.watch_id)


async def test_require_owned_watch_denies_a_missing_watch(watch_service: WatchService) -> None:
    update = _update_with_user(11)

    with pytest.raises(InputError):
        await require_owned_watch(update, watch_service, uuid4())


async def test_require_owned_watch_denies_an_update_with_no_user(
    watch_service: WatchService,
) -> None:
    watch = await watch_service.create(11, _criteria())
    update = _update_with_user(None)

    with pytest.raises(InputError):
        await require_owned_watch(update, watch_service, watch.watch_id)


async def test_require_owned_watch_denial_is_identical_for_missing_and_unowned(
    watch_service: WatchService,
) -> None:
    watch = await watch_service.create(11, _criteria())
    missing_error: str | None = None
    unowned_error: str | None = None
    try:
        await require_owned_watch(_update_with_user(11), watch_service, uuid4())
    except InputError as exc:
        missing_error = str(exc)
    try:
        await require_owned_watch(_update_with_user(22), watch_service, watch.watch_id)
    except InputError as exc:
        unowned_error = str(exc)

    assert missing_error is not None
    assert missing_error == unowned_error
