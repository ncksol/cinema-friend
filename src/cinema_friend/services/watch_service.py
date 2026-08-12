"""Owner-scoped watch lifecycle: create, list, read, pause, resume, delete.

Every method here opens its own connection and owns the transaction boundary around it;
:class:`~cinema_friend.storage.watch_repository.WatchRepository` methods only ever
consume the connection they are handed. That keeps this service, not the repository, in
charge of what counts as one unit of work -- e.g. pause's read-then-write must not be
split across two connections, where a concurrent delete could land in between.

No method ever distinguishes "no such watch" from "that watch belongs to someone else":
both raise the same :class:`InputError`, so a caller cannot use this service to probe
which watch IDs exist for other users.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import aiosqlite

from cinema_friend.bfi.urls import parse_article_url
from cinema_friend.clock import Clock
from cinema_friend.domain.errors import InputError
from cinema_friend.domain.state import WatchMode, WatchStatus
from cinema_friend.domain.watch import Watch, WatchCriteria
from cinema_friend.storage.database import Database
from cinema_friend.storage.watch_repository import WatchRepository

_NOT_FOUND = "watch not found"


class WatchService:
    """Ownership-scoped CRUD and scheduling over one owner's watches."""

    def __init__(self, database: Database, watches: WatchRepository, clock: Clock) -> None:
        self._database = database
        self._watches = watches
        self._clock = clock

    async def create(
        self, owner_user_id: int, criteria: WatchCriteria, *, watch_id: UUID | None = None
    ) -> Watch:
        """Persist a new watch, storing the article's canonical URL/slug.

        The caller's ``source_url``/``slug`` may be any accepted BFI shape (or may
        disagree with each other); :func:`parse_article_url` is the single source of
        truth for what the article actually is, so its result -- not the caller's -- is
        what gets stored. The watch is scheduled for an immediate check; ``title`` stays
        ``None`` until that check's BFI parse names the film.

        Passing ``watch_id`` makes the call idempotent: a caller that derives a stable
        identity from a durable setup record can retry after a crash without risking a
        second watch, because an existing row with that id is returned unchanged. An id
        belonging to another owner raises the usual not-found error rather than
        revealing that it exists.
        """
        article = parse_article_url(criteria.source_url)
        canonical_criteria = replace(criteria, source_url=article.canonical_url, slug=article.slug)
        now = self._clock.now()
        watch = Watch(
            watch_id=watch_id if watch_id is not None else uuid4(),
            user_id=owner_user_id,
            criteria=canonical_criteria,
            status=WatchStatus.ACTIVE,
            created_at=now,
            updated_at=now,
            next_check_at=now,
        )
        async with self._database.connection() as conn, self._database.transaction(conn):
            if watch_id is not None:
                existing = await self._watches.get(conn, watch_id)
                if existing is not None:
                    if existing.user_id != owner_user_id:
                        raise InputError(_NOT_FOUND)
                    return existing
            await self._watches.create(conn, watch)
        return watch

    async def list_for_owner(self, owner_user_id: int) -> tuple[Watch, ...]:
        async with self._database.connection() as conn:
            return await self._watches.list_for_owner(conn, owner_user_id)

    async def get_owned(self, owner_user_id: int, watch_id: UUID) -> Watch:
        async with self._database.connection() as conn:
            return await self._fetch_owned(conn, owner_user_id, watch_id)

    async def pause(self, owner_user_id: int, watch_id: UUID) -> Watch:
        """Stop scheduling checks for a watch its owner controls.

        Only an ``ACTIVE`` ``RECURRING`` watch can be paused -- a ``ONE_OFF`` watch was
        never eligible in the first place, and a watch already in a terminal state
        (``COMPLETED``/``EXPIRED``/``FAILED``) or already ``PAUSED`` has nothing to stop.
        Any of those cases raises the identical not-found error a missing or
        non-owned watch would, so an invalid operation on your own watch never reveals
        more than "that did not happen" -- the same guarantee ownership already gets.

        Clears ``next_check_at`` so a paused watch can never be picked up as due; only
        :meth:`resume` puts it back on the schedule.
        """
        async with self._database.connection() as conn, self._database.transaction(conn):
            watch = await self._fetch_owned(conn, owner_user_id, watch_id)
            if watch.criteria.mode is not WatchMode.RECURRING or watch.status is not WatchStatus.ACTIVE:
                raise InputError(_NOT_FOUND)
            paused = replace(
                watch,
                status=WatchStatus.PAUSED,
                next_check_at=None,
                updated_at=self._clock.now(),
            )
            await self._watches.update(conn, paused)
        return paused

    async def resume(self, owner_user_id: int, watch_id: UUID) -> Watch:
        """Reactivate a paused watch and schedule an immediate check.

        Only a ``PAUSED`` ``RECURRING`` watch can be resumed -- a ``ONE_OFF`` watch was
        never pausable, an already-``ACTIVE`` watch has nothing to resume, and a
        terminal-state watch (``COMPLETED``/``EXPIRED``/``FAILED``) is done. Any of
        those cases raises the same not-found error as a missing or non-owned watch,
        for the same reason :meth:`pause` does.

        Resuming, like creating, is the owner explicitly asking to hear the current
        state again, so the very next check is scheduled for now rather than waiting for
        whatever interval the watch would otherwise be on.
        """
        async with self._database.connection() as conn, self._database.transaction(conn):
            watch = await self._fetch_owned(conn, owner_user_id, watch_id)
            if watch.criteria.mode is not WatchMode.RECURRING or watch.status is not WatchStatus.PAUSED:
                raise InputError(_NOT_FOUND)
            now = self._clock.now()
            resumed = replace(
                watch,
                status=WatchStatus.ACTIVE,
                next_check_at=now,
                updated_at=now,
            )
            await self._watches.update(conn, resumed)
        return resumed

    async def delete(self, owner_user_id: int, watch_id: UUID) -> None:
        """Delete a watch in one transaction; ``ON DELETE CASCADE`` removes its rows."""
        async with self._database.connection() as conn, self._database.transaction(conn):
            await self._fetch_owned(conn, owner_user_id, watch_id)
            await self._watches.delete(conn, watch_id)

    async def _fetch_owned(
        self, conn: aiosqlite.Connection, owner_user_id: int, watch_id: UUID
    ) -> Watch:
        watch = await self._watches.get(conn, watch_id)
        if watch is None or watch.user_id != owner_user_id:
            raise InputError(_NOT_FOUND)
        return watch
