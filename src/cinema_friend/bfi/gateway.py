"""BFI availability gateway: pagination, request coalescing, and seat-map reads.

The transport (:mod:`cinema_friend.bfi.transport`) already owns retries, the
challenge circuit, and request spacing/concurrency. This gateway sits above
it and owns pagination assembly, per-URL single-flight + TTL document
coalescing, deduplication of performance rows, and cross-source validation
between the article listing and the seat-map page. It never caches a failed
fetch: only a successfully retrieved document is stored.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass
from typing import Protocol

from cinema_friend.bfi.article_context import ArticlePage, parse_article_page, performance_from_row
from cinema_friend.bfi.seat_map import KNOWN_STATUS_CODES, TAKEN_STATUS_CODES, parse_seat_map
from cinema_friend.bfi.transport import DocumentKind, FetchedDocument
from cinema_friend.bfi.urls import film_page_url, pagination_url
from cinema_friend.clock import Clock
from cinema_friend.domain.bfi import Performance, PerformanceListing, SeatMap, SeatStatus
from cinema_friend.domain.errors import BfiContractError

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_SECONDS = 60.0

_AVAILABLE_STATUS_CODE = "A"
"""The raw ``data-status`` value BFI counts in ``availability_num``.

The cross-source comparison is stated against this attribute rather than against
:attr:`SeatStatus.AVAILABLE`, because the seat-map parser reclassifies an accessible
space as ``RESTRICTED`` while BFI keeps counting it. Comparing BFI's number against
the *offerable* seats manufactures a contract error out of an ordinary near-sold-out
map -- which pauses the watch and alerts its owner about a site change that never
happened.
"""

# Absolute drift between BFI's reported availability_num and the count of
# seats this gateway parsed as raw-A above which the mismatch is logged
# (but not treated as fatal -- BFI's own counter can legitimately lag the
# seat-map SVG by a few seats under concurrent bookings).
_DRIFT_LOG_THRESHOLD = 5


@dataclass(frozen=True, slots=True)
class AvailabilityDrift:
    """Difference between BFI's reported ``availability_num`` and parsed seats.

    ``parsed`` counts raw ``data-status="A"`` circles, which is what BFI's own counter
    counts: an accessible space is included in ``availability_num`` even though this
    service will never offer it. ``offerable`` is the narrower count of seats a watch
    could actually be told about, carried alongside as a metric so a near-sold-out map
    whose last seats are all wheelchair spaces reads as what it is instead of as drift.

    ``difference`` is ``reported - parsed``: positive when BFI reports more
    availability than the seat map shows, negative when the seat map shows
    more available seats than BFI's counter reports.
    """

    reported: int
    parsed: int
    difference: int
    offerable: int


class DocumentTransport(Protocol):
    """The subset of :class:`~cinema_friend.bfi.transport.BfiTransport` the gateway needs."""

    async def get(self, url: str, kind: DocumentKind) -> FetchedDocument: ...


class BfiGateway:
    """Assembles paginated performance listings and cross-checked seat maps.

    Concurrency model:

    - ``_lock`` protects both ``_inflight`` and ``_cache``. It is held only
      for the read-check-write bookkeeping around a fetch, never for the
      duration of the outbound request itself, so unrelated URLs and
      concurrent readers of different documents never block on each other.
    - A document fetch already under way for a URL is shared: a second
      caller awaits the same :class:`asyncio.Task` instead of issuing a
      second request (single-flight). A completed fetch is served from
      ``_cache`` until ``cache_seconds`` elapses, measured via ``clock``.
    - Only a *successful* fetch is cached; an exception (including a
      challenge) propagates to every awaiter and leaves nothing behind for
      a later caller to reuse. ``_inflight`` is always cleaned up, on
      success or failure, so a subsequent request for the same URL is never
      wedged waiting on a task that has already finished.
    """

    def __init__(
        self,
        transport: DocumentTransport,
        clock: Clock,
        cache_seconds: float = _DEFAULT_CACHE_SECONDS,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Task[FetchedDocument]] = {}
        self._cache: dict[str, tuple[float, FetchedDocument]] = {}

    async def list_performances(self, slug: str) -> PerformanceListing:
        """Fetch and assemble every paginated performance row for *slug*.

        Deduplicates rows by ``performance_id`` (an exact repeat, e.g. from
        overlapping pages, is collapsed to one entry) and raises
        :class:`BfiContractError` if two rows share an ID but disagree on
        the parsed data, if ``article_id``/``total_pages`` changes between
        pages of the same listing, or if a page after the first carries no
        rows at all.

        An empty later page is a contract error rather than the end of the
        listing. The chain length comes from page 1's ``total_pages``, so a
        structurally valid page that yields nothing means the listing was
        truncated somewhere between BFI and this parser -- and a truncated
        listing is indistinguishable, downstream, from a film that simply
        has fewer screenings. Page 1 is exempt: a film with nothing on sale
        is the one legitimate way to see no rows.
        """
        url = film_page_url(slug)
        document = await self._fetch_document(url, DocumentKind.ARTICLE)
        first_page = parse_article_page(document.text)

        performances: dict[str, Performance] = {}
        self._collect_page(first_page, performances)
        title = first_page.title

        for page_number in range(2, first_page.total_pages + 1):
            page_url = pagination_url(first_page.s_token, page_number, first_page.article_id)
            next_document = await self._fetch_document(page_url, DocumentKind.ARTICLE)
            page = parse_article_page(next_document.text)
            _validate_pagination_identity(first_page, page, page_number)
            if not page.rows:
                raise BfiContractError(
                    f"page {page_number} of {first_page.total_pages} carried no rows; "
                    "the listing is truncated"
                )
            title = _merge_title(title, page.title, page_number)
            self._collect_page(page, performances)

        return PerformanceListing(title=title, performances=tuple(performances.values()))

    async def load_seat_map(self, performance: Performance) -> SeatMap:
        """Fetch the seat map for *performance* and cross-check availability.

        Raises :class:`BfiContractError` if the seat map's embedded
        performance identity does not match *performance* (checked by
        :func:`~cinema_friend.bfi.seat_map.parse_seat_map`), or if BFI
        reports positive availability while the map carries no raw-``A``
        seat *and* some seat carries a status code this parser does not
        recognise. Zero raw-``A`` seats is not on its own a site change:
        the listing and the map are two documents read up to a cache
        lifetime apart, and the last free seats can be bought (``S``) or
        merely taken into another customer's basket (``O``) in between.
        A map that carries no ``A``, ``S`` or ``O`` seat at all cannot
        mean "just sold out" either, so it is logged at warning level --
        but it still does not fail the read, because stopping a watch
        says the site changed, and this evidence does not show that. A
        larger, non-zero drift is logged but does not fail the read, and
        a map whose available seats are all restricted is a logged metric
        rather than any kind of failure.
        """
        if performance.seat_map_url is None:
            raise BfiContractError(
                f"performance {performance.performance_id!r} has no seat_map_url"
            )
        document = await self._fetch_document(performance.seat_map_url, DocumentKind.SEAT_MAP)
        seat_map = parse_seat_map(document.text, performance.performance_id)
        drift = _availability_drift(performance, seat_map)

        if drift.parsed == 0 and performance.availability_num > 0:
            codes = {seat.raw_status_code for seat in seat_map.seats}
            unrecognised = sorted(codes - KNOWN_STATUS_CODES)
            if unrecognised:
                raise BfiContractError(
                    f"performance {performance.performance_id!r} reports "
                    f"availability_num={performance.availability_num} but zero available "
                    f"seats parsed, and the map carries unrecognised status codes "
                    f"{unrecognised}"
                )
            if codes.isdisjoint(TAKEN_STATUS_CODES):
                logger.warning(
                    "performance %s reports availability_num=%d but its seat map carries "
                    "no available, sold or held seat: %s",
                    performance.performance_id,
                    drift.reported,
                    sorted(codes),
                )
            else:
                logger.info(
                    "performance %s sold out between its listing and its seat map: "
                    "reported=%d parsed=0",
                    performance.performance_id,
                    drift.reported,
                )
        if abs(drift.difference) > _DRIFT_LOG_THRESHOLD:
            logger.warning(
                "availability drift for performance %s: reported=%d parsed=%d "
                "difference=%d offerable=%d",
                performance.performance_id,
                drift.reported,
                drift.parsed,
                drift.difference,
                drift.offerable,
            )
        elif drift.parsed > 0 and drift.offerable == 0:
            logger.info(
                "every available seat for performance %s is restricted: parsed=%d offerable=0",
                performance.performance_id,
                drift.parsed,
            )
        return seat_map

    @staticmethod
    def _collect_page(page: ArticlePage, performances: dict[str, Performance]) -> None:
        for row in page.rows:
            if row.get("object_type") != "P":
                continue
            performance = performance_from_row(row)
            existing = performances.get(performance.performance_id)
            if existing is None:
                performances[performance.performance_id] = performance
            elif existing != performance:
                raise BfiContractError(
                    f"duplicate performance {performance.performance_id!r} "
                    "reports conflicting data across pages"
                )

    async def _fetch_document(self, url: str, kind: DocumentKind) -> FetchedDocument:
        async with self._lock:
            cached = self._cache.get(url)
            if cached is not None:
                expires_at, document = cached
                if self._clock.monotonic() < expires_at:
                    return document
                del self._cache[url]
            task = self._inflight.get(url)
            if task is None:
                task = asyncio.ensure_future(self._transport.get(url, kind))
                # Bookkeeping (inflight removal, TTL caching) lives in a
                # done-callback tied to the shared task itself, never to any
                # one awaiter's `finally`. `asyncio.Task.cancel()` cancels
                # whatever future the task is currently blocked on -- which,
                # for a plain `await task`, would be this very shared task --
                # so a second, uncancelled caller's `await` on the same task
                # would be corrupted by a first caller's unrelated
                # cancellation. `asyncio.shield` below stops that
                # propagation; the callback then makes cleanup independent
                # of which (if any) awaiter is still around to observe it.
                task.add_done_callback(functools.partial(self._on_fetch_done, url))
                self._inflight[url] = task

        return await asyncio.shield(task)

    def _on_fetch_done(self, url: str, task: asyncio.Task[FetchedDocument]) -> None:
        """Clean up `_inflight` and populate `_cache` once a shared fetch finishes.

        Runs synchronously as an `asyncio` callback (never suspends), so it
        always completes atomically between coroutine steps and cannot
        interleave with another coroutine's `_lock`-held critical section --
        no `async with self._lock` is needed here. Only a genuinely
        successful fetch is cached; a cancelled or failed one is dropped so
        the next caller retries from scratch.
        """
        if self._inflight.get(url) is task:
            del self._inflight[url]
        if task.cancelled() or task.exception() is not None:
            return
        document = task.result()
        self._cache[url] = (self._clock.monotonic() + self._cache_seconds, document)


def _validate_pagination_identity(first: ArticlePage, page: ArticlePage, page_number: int) -> None:
    if page.article_id != first.article_id:
        raise BfiContractError(
            f"page {page_number} article_id changed: "
            f"expected {first.article_id!r}, got {page.article_id!r}"
        )
    if page.total_pages != first.total_pages:
        raise BfiContractError(
            f"page {page_number} total_pages changed: "
            f"expected {first.total_pages}, got {page.total_pages}"
        )


def _merge_title(current: str | None, page_title: str | None, page_number: int) -> str | None:
    if not page_title:
        return current
    if current is None or current == page_title:
        return page_title
    raise BfiContractError(
        f"page {page_number} title changed: expected {current!r}, got {page_title!r}"
    )


def _availability_drift(performance: Performance, seat_map: SeatMap) -> AvailabilityDrift:
    parsed = sum(1 for seat in seat_map.seats if seat.raw_status_code == _AVAILABLE_STATUS_CODE)
    offerable = sum(1 for seat in seat_map.seats if seat.status is SeatStatus.AVAILABLE)
    reported = performance.availability_num
    return AvailabilityDrift(
        reported=reported,
        parsed=parsed,
        difference=reported - parsed,
        offerable=offerable,
    )
