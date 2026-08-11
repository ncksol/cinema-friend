"""Run one availability check end to end and commit everything it decided at once.

A check is a fan-out over the network followed by a small burst of related writes:
the check run itself, the snapshot of what was on sale, the watch's new status and
next run time, and the message the user should receive. Those writes only make sense
together. A snapshot with no delivery silently swallows a result; a delivery with no
snapshot points at nothing; a rescheduled watch with no check run loses the audit
trail. So they all land inside a single transaction, and any failure inside it takes
the whole set back out.

The network work deliberately happens *before* that transaction opens. SQLite writers
are exclusive, and a check spends seconds waiting on the BFI, so holding the write
lock across the fetch would serialise every other watch in the process behind one slow
host. ``started_at`` is captured before the fetch, so the persisted check run still
records when the work really began rather than when it finished.

That window is long enough for the watch to be paused, deleted, or edited under a
running check, so the watch is re-read and re-compared inside the transaction before
anything is written, and the check is abandoned if it no longer matches. The lock this
service does not hold during the fetch is exactly the lock a concurrent lifecycle
action needs, and the point of not holding it is that those actions get to win.

One piece of state deliberately escapes the transaction: the host circuit breaker.
The transport owns it and writes it through its own connection, precisely so that a
rolled-back check cannot also roll back the evidence that a host is unhealthy. This
service only ever reads the circuit, and reads it outside the transaction so it never
contends with its own writer.
"""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

import aiosqlite

from cinema_friend.clock import Clock
from cinema_friend.domain.bfi import Performance, SeatMap
from cinema_friend.domain.errors import (
    BfiChallengeError,
    BfiContractError,
    BfiNetworkError,
    CircuitOpenError,
    ConflictError,
    InputError,
    PersistenceError,
)
from cinema_friend.domain.results import (
    CheckResult,
    HostCircuit,
    NotificationPayload,
    RankedOption,
)
from cinema_friend.domain.state import CheckOutcome, CheckTrigger, WatchMode, WatchStatus
from cinema_friend.domain.watch import Watch
from cinema_friend.services.notification_policy import (
    QueuedNotification,
    circuit_records_incident,
    decide_contract_error_notification,
    decide_degradation_notification,
    decide_recovery_notification,
    decide_result_notification,
)
from cinema_friend.storage.database import Database
from cinema_friend.storage.notification_repository import NotificationRepository
from cinema_friend.storage.result_repository import ResultRepository
from cinema_friend.storage.watch_repository import WatchRepository
from cinema_friend.watches.criteria import performance_matches
from cinema_friend.watches.ranking import rank_options

logger = logging.getLogger(__name__)

_NOT_FOUND = "watch not found"
_RESULTS_KIND = "results"

#: Raised when the watch changed under a check that had already read it. The check is
#: abandoned rather than reconciled: it ranked against criteria, and decided a schedule,
#: that no longer describe the watch it would be writing to.
_CONFLICT = "watch changed while its check was running"

#: Upper bound on the proportional jitter added to a recurring watch's interval, so a
#: fleet of watches created together does not stay in lockstep against one host.
_MAX_SCHEDULE_JITTER = 0.1

#: Used when a host failure gives us no probe time to wait for. Mirrors the transport's
#: first circuit delay, which is the shortest interval anyone has judged safe to retry.
_FALLBACK_BACKOFF = timedelta(minutes=15)


class AvailabilityGateway(Protocol):
    """The slice of the BFI gateway a check needs.

    Narrower than the concrete gateway on purpose: it keeps this service out of the
    transport's dependency graph and makes the fetch trivially substitutable in tests.
    """

    async def list_performances(self, slug: str) -> tuple[Performance, ...]: ...

    async def load_seat_map(self, performance: Performance) -> SeatMap: ...


class CircuitReader(Protocol):
    """Read-only view of persisted host circuit state.

    Read-only by design. The transport is the only component allowed to advance a
    circuit; a check merely consults it to learn when a degraded host is worth another
    attempt, and to name the generation an alert belongs to.
    """

    async def load(self, host: str) -> HostCircuit: ...


class CheckService:
    """Fetch availability for one watch and commit the whole outcome atomically."""

    def __init__(
        self,
        database: Database,
        gateway: AvailabilityGateway,
        circuits: CircuitReader,
        watches: WatchRepository,
        results: ResultRepository,
        notifications: NotificationRepository,
        clock: Clock,
        jitter_source: Callable[[], float] | None = None,
    ) -> None:
        self._database = database
        self._gateway = gateway
        self._circuits = circuits
        self._watches = watches
        self._results = results
        self._notifications = notifications
        self._clock = clock
        self._jitter = jitter_source or (lambda: random.uniform(0.0, _MAX_SCHEDULE_JITTER))

    async def check(self, watch_id: UUID, trigger: CheckTrigger) -> CheckResult:
        """Run a check for ``watch_id`` and persist everything it produced.

        Every anticipated BFI failure is translated into a recorded outcome and a new
        watch state rather than an exception, because a failed check is a normal event
        the scheduler must be able to act on. Anything unanticipated propagates
        untouched: laundering an unknown defect into a tidy ``CheckResult`` would hide
        a bug behind a row that claims the system merely had a bad network day.

        ``ConflictError`` propagates for the same reason. A watch that changed mid-check
        has no outcome to record -- the run was abandoned, not failed -- and the caller
        needs to know the difference between a check that decided something and one that
        found it was no longer entitled to.
        """
        watch = await self._load_watch(watch_id)
        started_at = self._clock.now()
        try:
            candidates, maps = await self._fetch(watch)
        except BfiChallengeError as error:
            return await self._persist_challenge(watch, trigger, started_at, error)
        except CircuitOpenError as error:
            return await self._persist_circuit_open(watch, trigger, started_at, error)
        except BfiContractError as error:
            return await self._persist_contract_error(watch, trigger, started_at, error)
        except BfiNetworkError as error:
            return await self._persist_network_error(watch, trigger, started_at, error)
        options = rank_options(watch.criteria, tuple(zip(candidates, maps, strict=True)))
        return await self._persist_success(watch, trigger, started_at, candidates, options)

    async def _load_watch(self, watch_id: UUID) -> Watch:
        try:
            async with self._database.connection() as conn:
                watch = await self._watches.get(conn, watch_id)
        except sqlite3.Error as error:
            raise PersistenceError(str(error)) from error
        if watch is None:
            raise InputError(_NOT_FOUND)
        return watch

    async def _fetch(self, watch: Watch) -> tuple[tuple[Performance, ...], tuple[SeatMap, ...]]:
        """Fetch the listing, discard what cannot match, then load the survivors' maps.

        Eligibility is applied before any seat map is requested. Filtering afterwards
        would be identical in outcome and far more expensive: a listing page routinely
        holds dozens of performances, of which a single watch usually wants one or two.
        """
        performances = await self._gateway.list_performances(watch.criteria.slug)
        candidates = tuple(
            performance
            for performance in performances
            if performance_matches(watch.criteria, performance)
        )
        return candidates, await self._load_seat_maps(candidates)

    async def _load_seat_maps(self, candidates: Sequence[Performance]) -> tuple[SeatMap, ...]:
        """Load every candidate's seat map concurrently, surfacing the first failure.

        ``return_exceptions`` is set so a single bad performance cannot leave its
        siblings' tasks orphaned and their exceptions unretrieved. The results are then
        scanned in candidate order, which makes the error a caller sees deterministic
        rather than a race between coroutines.
        """
        if not candidates:
            return ()
        loaded: list[SeatMap | BaseException] = list(
            await asyncio.gather(
                *(self._gateway.load_seat_map(performance) for performance in candidates),
                return_exceptions=True,
            )
        )
        maps: list[SeatMap] = []
        for item in loaded:
            if isinstance(item, BaseException):
                raise item
            maps.append(item)
        return tuple(maps)

    async def _persist_success(
        self,
        watch: Watch,
        trigger: CheckTrigger,
        started_at: datetime,
        candidates: Sequence[Performance],
        options: Sequence[RankedOption],
    ) -> CheckResult:
        """Store the snapshot, reschedule the watch, and queue any message it earned.

        A snapshot is written on every success, including an empty one. "Nothing is
        available right now" is a real answer that the watch list and the next check's
        comparison both depend on; skipping it would leave a stale snapshot masquerading
        as current. The *delivery* is what the notification policy gates, so a check that
        found nothing new writes a fresh snapshot and stays silent.
        """
        completed_at = self._clock.now()
        # Loaded before the transaction opens, and only when it can matter: the recovery
        # alert has to name the generation the host was in when it broke.
        circuit = (
            await self._circuits.load(_host_of(watch)) if trigger is CheckTrigger.RECOVERY else None
        )
        async with self._write(watch) as conn:
            check_run_id = await self._results.start_check(
                conn, watch_id=watch.watch_id, trigger=trigger, started_at=started_at
            )
            known_keys = await self._notifications.known_keys(conn, watch.watch_id)
            state = await self._notifications.state(conn, watch.watch_id)
            decision = decide_result_notification(
                trigger, options, known_keys, state.last_best_rank, watch.user_id
            )
            snapshot = await self._results.complete_with_snapshot(
                conn,
                check_run_id=check_run_id,
                watch_id=watch.watch_id,
                options=options,
                checked_at=completed_at,
                outcome=CheckOutcome.SUCCESS,
                performance_count=len(candidates),
            )
            await self._watches.update(conn, self._reschedule(watch, trigger, completed_at))
            if decision.requires_snapshot:
                await self._notifications.create_delivery(
                    conn,
                    f"{_RESULTS_KIND}:{watch.watch_id}:{snapshot.snapshot_id}",
                    NotificationPayload(
                        kind=decision.kind,
                        recipient_user_id=decision.recipient_user_id,
                        watch_id=watch.watch_id,
                        snapshot_id=snapshot.snapshot_id,
                        new_option_count=len(decision.new_option_keys),
                        host=None,
                        recovery_text=None,
                    ),
                    completed_at,
                )
            if circuit is not None and circuit_records_incident(circuit):
                owners = await self._watches.list_active_owner_ids(conn, circuit.host)
                await self._queue_alerts(
                    conn, decide_recovery_notification(circuit, owners), completed_at
                )
        return CheckResult(
            check_run_id=check_run_id,
            watch_id=watch.watch_id,
            trigger=trigger,
            outcome=CheckOutcome.SUCCESS,
            snapshot_id=snapshot.snapshot_id,
            performance_count=len(candidates),
            option_count=len(options),
            error_detail=None,
        )

    async def _persist_challenge(
        self, watch: Watch, trigger: CheckTrigger, started_at: datetime, error: BfiChallengeError
    ) -> CheckResult:
        """Back the watch off to the host's own probe time and tell affected owners once.

        The circuit is the authority on when the host is worth touching again, so the
        watch adopts its probe time verbatim instead of inventing a schedule that would
        march straight back into the block. The alert is per distinct owner per circuit
        generation, so a user with five watches on a broken host hears about it once.
        """
        circuit = await self._circuits.load(_host_of(watch))
        return await self._persist_failure(
            watch,
            trigger,
            started_at,
            completed_at=self._clock.now(),
            outcome=CheckOutcome.CHALLENGE,
            error_kind="challenge",
            error=error,
            status=WatchStatus.BACKOFF,
            next_check_at=circuit.next_probe,
            degraded_circuit=circuit,
        )

    async def _persist_circuit_open(
        self, watch: Watch, trigger: CheckTrigger, started_at: datetime, error: CircuitOpenError
    ) -> CheckResult:
        """Back off without alerting: the alert was already sent when the host broke.

        An open circuit means the request never left the process. The generation that
        opened it already notified everyone affected, so re-announcing it here would
        turn one outage into a message per watch per cycle.
        """
        circuit = await self._circuits.load(_host_of(watch))
        return await self._persist_failure(
            watch,
            trigger,
            started_at,
            completed_at=self._clock.now(),
            outcome=CheckOutcome.CIRCUIT_OPEN,
            error_kind="circuit_open",
            error=error,
            status=WatchStatus.BACKOFF,
            next_check_at=circuit.next_probe,
        )

    async def _persist_contract_error(
        self, watch: Watch, trigger: CheckTrigger, started_at: datetime, error: BfiContractError
    ) -> CheckResult:
        """Pause the watch, keep its last good snapshot, and tell its owner once.

        Retrying cannot help. Either the site changed shape or the watch points at
        something that is not a film page, and both need a human. Pausing clears
        ``next_run_at`` so the scheduler stops burning requests on it, while the last
        good snapshot is left in place so the user can still see what was found.

        A paused watch that says nothing is indistinguishable from a working one that
        keeps finding nothing, so the owner is alerted -- once per watch, whatever the
        parse does next -- in the same transaction that pauses it. A pause nobody was
        told about, or an alert about a watch that is still running, are both wrong.
        """
        logger.warning("watch %s paused after contract error: %s", watch.watch_id, error)
        return await self._persist_failure(
            watch,
            trigger,
            started_at,
            completed_at=self._clock.now(),
            outcome=CheckOutcome.CONTRACT_ERROR,
            error_kind="contract",
            error=error,
            status=WatchStatus.PAUSED,
            next_check_at=None,
            owner_alerts=(decide_contract_error_notification(watch.watch_id, watch.user_id),),
        )

    async def _persist_network_error(
        self, watch: Watch, trigger: CheckTrigger, started_at: datetime, error: BfiNetworkError
    ) -> CheckResult:
        """Retry a recurring watch on its normal cadence; fail a one-off outright.

        A transient network fault says nothing about the watch, so a recurring one keeps
        its ordinary rhythm rather than being punished with a special penalty interval.
        A one-off has no next cadence to fall back on, so it is marked failed and left
        for its owner to retrigger.
        """
        recurring = watch.criteria.mode is WatchMode.RECURRING
        completed_at = self._clock.now()
        return await self._persist_failure(
            watch,
            trigger,
            started_at,
            completed_at=completed_at,
            outcome=CheckOutcome.NETWORK_ERROR,
            error_kind="network",
            error=error,
            status=WatchStatus.BACKOFF if recurring else WatchStatus.FAILED,
            next_check_at=self._next_run(watch, completed_at) if recurring else None,
        )

    async def _persist_failure(
        self,
        watch: Watch,
        trigger: CheckTrigger,
        started_at: datetime,
        *,
        completed_at: datetime,
        outcome: CheckOutcome,
        error_kind: str,
        error: Exception,
        status: WatchStatus,
        next_check_at: datetime | None,
        degraded_circuit: HostCircuit | None = None,
        owner_alerts: Sequence[QueuedNotification] = (),
    ) -> CheckResult:
        """Record the failed run, the watch's new state, and any alert, in one transaction."""
        if status is WatchStatus.BACKOFF and next_check_at is None:
            # Only reachable if a host failure surfaced without an open circuit behind
            # it. Leaving next_run_at unset would strand the watch permanently.
            next_check_at = completed_at + _FALLBACK_BACKOFF
        async with self._write(watch) as conn:
            check_run_id = await self._results.start_check(
                conn, watch_id=watch.watch_id, trigger=trigger, started_at=started_at
            )
            await self._results.fail_check(
                conn,
                check_run_id=check_run_id,
                outcome=outcome,
                error_kind=error_kind,
                error_message=str(error),
                completed_at=completed_at,
            )
            await self._watches.update(
                conn,
                replace(
                    watch,
                    status=status,
                    next_check_at=next_check_at,
                    last_check_at=completed_at,
                    updated_at=completed_at,
                ),
            )
            if degraded_circuit is not None:
                owners = await self._watches.list_active_owner_ids(conn, degraded_circuit.host)
                await self._queue_alerts(
                    conn, decide_degradation_notification(degraded_circuit, owners), completed_at
                )
            await self._queue_alerts(conn, owner_alerts, completed_at)
        return CheckResult(
            check_run_id=check_run_id,
            watch_id=watch.watch_id,
            trigger=trigger,
            outcome=outcome,
            snapshot_id=None,
            performance_count=0,
            option_count=0,
            error_detail=str(error),
        )

    async def _queue_alerts(
        self,
        conn: aiosqlite.Connection,
        alerts: Sequence[QueuedNotification],
        now: datetime,
    ) -> None:
        for alert in alerts:
            await self._notifications.create_delivery(
                conn, alert.idempotency_key, alert.payload, now
            )

    def _reschedule(self, watch: Watch, trigger: CheckTrigger, completed_at: datetime) -> Watch:
        """Return the watch as it should look after a successful check.

        A one-off is finished the moment it succeeds. A manual check keeps whatever the
        watch was already going to do next, so asking "check now" cannot be used to
        quietly drag a watch's schedule around. Everything else, including the check a
        watch gets at creation and the probe that brings it back from backoff, resets to
        a fresh interval from now.
        """
        if watch.criteria.mode is WatchMode.ONE_OFF:
            return replace(
                watch,
                status=WatchStatus.COMPLETED,
                next_check_at=None,
                last_check_at=completed_at,
                updated_at=completed_at,
            )
        next_check_at = (
            watch.next_check_at
            if trigger is CheckTrigger.MANUAL
            else self._next_run(watch, completed_at)
        )
        return replace(
            watch,
            status=WatchStatus.ACTIVE,
            next_check_at=next_check_at,
            last_check_at=completed_at,
            updated_at=completed_at,
        )

    def _next_run(self, watch: Watch, completed_at: datetime) -> datetime:
        interval = watch.criteria.interval
        if interval is None:  # pragma: no cover - recurring criteria always carry one
            raise InputError(f"recurring watch {watch.watch_id} has no interval")
        return completed_at + interval + interval * self._jitter()

    @asynccontextmanager
    async def _write(self, observed: Watch) -> AsyncIterator[aiosqlite.Connection]:
        """Open the one transaction a check commits through, if the watch still matches.

        A check reads its watch, then spends seconds on the network with nothing held.
        A pause, a delete, an edit, or another check can all land in that window, so the
        watch is re-read as the first statement inside the transaction and compared with
        what this check actually worked from. Any difference at all -- status, criteria,
        schedule, or a row that has since been deleted -- aborts before a single write.

        ``BEGIN IMMEDIATE`` takes the write lock before that re-read, so nothing can slip
        between the comparison and the writes it authorises. Aborting raises rather than
        reconciling: the results were ranked against criteria, and the schedule computed
        from an interval, that may no longer be the watch's. Writing them anyway would
        undo a deliberate lifecycle action -- reviving a paused watch, or resurrecting a
        deleted one as a foreign-key error at best.

        Repository transactions nest as savepoints inside this one, so each repository
        keeps its own atomicity guarantee while the check as a whole still commits or
        vanishes as a unit. Storage faults are re-raised as ``PersistenceError`` so
        callers see one typed failure rather than a driver exception leaking upward.
        """
        try:
            async with self._database.connection() as conn, self._database.transaction(conn):
                current = await self._watches.get(conn, observed.watch_id)
                if current != observed:
                    raise ConflictError(f"{_CONFLICT}: {observed.watch_id}")
                yield conn
        except sqlite3.Error as error:
            raise PersistenceError(str(error)) from error


def _host_of(watch: Watch) -> str:
    host = urlsplit(watch.criteria.source_url).hostname
    if host is None:  # pragma: no cover - validated when the watch is created
        raise InputError(f"watch {watch.watch_id} has no host in its source URL")
    return host
