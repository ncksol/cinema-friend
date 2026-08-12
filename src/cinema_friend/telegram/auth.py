"""Telegram-facing authorization: allow-list membership and watch ownership.

Every denial here looks identical regardless of cause -- a missing user, a user absent
from the allow list, a missing watch, or a watch owned by someone else. None of that
ever leaks: exposing *why* access failed would let an unauthorized caller enumerate
valid user IDs or watch IDs by trial and error.
"""

from __future__ import annotations

from collections.abc import Collection
from uuid import UUID

from telegram import Update

from cinema_friend.domain.errors import InputError
from cinema_friend.domain.watch import Watch
from cinema_friend.services.watch_service import WatchService

_DENIED = "not authorized"


def authorized_user_id(update: Update, allowed_ids: Collection[int]) -> int:
    """Return the update's Telegram user ID if it is present and allow-listed.

    Raises :class:`InputError` with an identical message for a missing user and for a
    user id absent from *allowed_ids*, so a caller cannot distinguish the two cases.
    """
    user = update.effective_user
    if user is None or user.id not in allowed_ids:
        raise InputError(_DENIED)
    return user.id


async def require_owned_watch(
    update: Update, watch_service: WatchService, watch_id: UUID
) -> Watch:
    """Return the watch identified by *watch_id* if the update's user owns it.

    The "does this watch belong to this user" question is delegated to
    :meth:`WatchService.get_owned`, which already raises the same error for a missing
    watch and for a watch owned by someone else; this function only adds the same
    generic denial when the update carries no user at all.
    """
    user = update.effective_user
    if user is None:
        raise InputError(_DENIED)
    return await watch_service.get_owned(user.id, watch_id)
