"""One bounded, read-only contract check against the live BFI site.

Every parser in this package is proven against synthetic fixtures, which proves the
parsers self-consistent and nothing else. This command is the only thing that asks the
real site whether those fixtures still describe it, and it is deliberately the smallest
question that can detect drift:

    one film page + its pagination chain + one seat map

It reads exactly one performance's seat map, chosen as the first on-sale reserved
performance with availability, and then asserts the one invariant that two independently
produced BFI documents must satisfy at the same instant: ``availability_num`` from the
listing equals the number of ``data-status="A"`` seats in the seat map. Because the two
reads happen seconds apart, the service tolerates small drift at runtime; here they are
read back-to-back and the equality is exact, which makes this the cheapest available
detector of a change in either parser.

What it deliberately does not do:

- It never requests the aggregate programme, and never looks at a second performance.
- It never writes: no database, no circuit-breaker row, no seat held, nothing reserved.
- It never polls, and is not part of the automated suite.
- It prints counts and statuses only. The transient ``sToken`` and the fetched documents
  are never printed, because the terminal it runs in is nobody's idea of a secret store.

Exit codes:

===== ==========================================================================
``0`` Contract holds.
``2`` The supplied URL is not a BFI film page, or no eligible performance exists.
``3`` BFI challenged the request, or the host circuit refused it.
``4`` Contract mismatch: a status, a field, or the availability equality.
``5`` Network failure.
===== ==========================================================================
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from cinema_friend.bfi.gateway import BfiGateway, DocumentTransport
from cinema_friend.bfi.transport import (
    AsyncHttpSession,
    BfiTransport,
    DocumentKind,
    FetchedDocument,
)
from cinema_friend.bfi.urls import parse_article_url
from cinema_friend.clock import Clock, SystemClock
from cinema_friend.config import Settings
from cinema_friend.domain.bfi import Performance, PerformanceListing, SeatMap, SeatStatus
from cinema_friend.domain.errors import (
    BfiChallengeError,
    BfiContractError,
    CircuitOpenError,
    InputError,
)
from cinema_friend.domain.results import CircuitState, HostCircuit
from cinema_friend.watches.criteria import is_on_sale

EXIT_OK: Final = 0
EXIT_INPUT: Final = 2
EXIT_CHALLENGE: Final = 3
EXIT_CONTRACT: Final = 4
EXIT_NETWORK: Final = 5

AVAILABLE_STATUS_CODE: Final = "A"
"""The raw ``data-status`` value BFI uses for a seat that can still be bought.

The contract is stated against the raw attribute rather than the parsed
:class:`~cinema_friend.domain.bfi.SeatStatus`, because the parser reclassifies an
accessible-space seat as ``RESTRICTED`` while BFI still counts it in ``availability_num``.
"""

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

_UNUSED_DATABASE_PATH: Final = Path("/dev/null")
"""Placeholder for the one :class:`Settings` field the transport never reads.

The smoke command opens no database. It builds a :class:`Settings` only because the
shared transport takes one, and it uses the same defaults the service runs with so the
request shaping under test is the request shaping in production.
"""


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SmokeReport:
    """What one bounded contract check observed. Counts and statuses only."""

    profile: str
    slug: str
    article_pages: int
    seat_maps: int
    performances: int
    eligible_performances: int
    performance_id: str
    performance_start: datetime
    reported_availability: int
    seats_parsed: int
    available_seats: int
    offerable_seats: int
    price_zones: int
    seat_statuses: tuple[tuple[str, int], ...]
    bytes_fetched: int

    def render(self) -> str:
        """Format the report for a terminal, carrying no token and no document text."""
        statuses = " ".join(f"{code}={count}" for code, count in self.seat_statuses)
        lines = [
            f"profile: {self.profile}",
            f"film: {self.slug}",
            f"article pages fetched: {self.article_pages}",
            f"performances parsed: {self.performances}",
            f"eligible performances: {self.eligible_performances}",
            f"performance: {self.performance_id}",
            f"performance start: {self.performance_start:%Y-%m-%d %H:%M %Z}",
            f"reported availability: {self.reported_availability}",
            f"seat maps fetched: {self.seat_maps}",
            f"seats parsed: {self.seats_parsed}",
            f"available seats: {self.available_seats}",
            f"offerable seats: {self.offerable_seats}",
            f"price zones: {self.price_zones}",
            f"seat statuses: {statuses}",
            f"bytes fetched: {self.bytes_fetched}",
            "contract: OK",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Collaborators the smoke run owns for the length of one check
# ---------------------------------------------------------------------------


class _EphemeralCircuitStore:
    """The host circuit for one smoke run, held in memory and then discarded.

    The service's circuit state is shared operational state: a smoke run that tripped it
    would suspend every real watch, and a smoke run that closed it would erase a genuine
    outage's backoff ladder. The check is read-only against BFI and read-only against the
    service, so it brings its own store and starts from a closed circuit every time.
    """

    def __init__(self) -> None:
        self._circuits: dict[str, HostCircuit] = {}

    async def load(self, host: str) -> HostCircuit:
        circuit = self._circuits.get(host)
        if circuit is not None:
            return circuit
        return HostCircuit(
            host=host,
            state=CircuitState.CLOSED,
            backoff_step=0,
            generation=0,
            next_probe=None,
            updated_at=_EPOCH,
            revision=0,
        )

    async def save(self, circuit: HostCircuit) -> None:
        self._circuits[circuit.host] = _with_revision(circuit, circuit.revision + 1)

    async def compare_and_swap(self, circuit: HostCircuit) -> HostCircuit | None:
        existing = self._circuits.get(circuit.host)
        stored_revision = 0 if existing is None else existing.revision
        if stored_revision != circuit.revision:
            return None
        written = _with_revision(circuit, circuit.revision + 1)
        self._circuits[circuit.host] = written
        return written


def _with_revision(circuit: HostCircuit, revision: int) -> HostCircuit:
    return HostCircuit(
        host=circuit.host,
        state=circuit.state,
        backoff_step=circuit.backoff_step,
        generation=circuit.generation,
        next_probe=circuit.next_probe,
        updated_at=circuit.updated_at,
        revision=revision,
    )


@dataclass(frozen=True, slots=True)
class _FetchRecord:
    kind: DocumentKind
    status_code: int
    byte_count: int


class _RecordingTransport:
    """Counts what the gateway actually fetched, so the check can bound and verify it.

    The gateway assembles pagination internally and the transport already refuses a
    non-success response, so neither can answer "how many documents went over the wire,
    and was every one of them a 200?". This sits between them and can.
    """

    def __init__(self, inner: DocumentTransport) -> None:
        self._inner = inner
        self.records: list[_FetchRecord] = []

    async def get(self, url: str, kind: DocumentKind) -> FetchedDocument:
        document = await self._inner.get(url, kind)
        self.records.append(
            _FetchRecord(
                kind=kind,
                status_code=document.status_code,
                byte_count=document.byte_count,
            )
        )
        return document

    def count(self, kind: DocumentKind) -> int:
        return sum(1 for record in self.records if record.kind is kind)

    @property
    def bytes_fetched(self) -> int:
        return sum(record.byte_count for record in self.records)

    def require_all_ok(self) -> None:
        for record in self.records:
            if record.status_code != 200:
                raise BfiContractError(
                    f"{record.kind.value} responded {record.status_code}, not 200"
                )


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


async def run_smoke(
    film_url: str,
    *,
    profile: str = "chrome",
    session: AsyncHttpSession | None = None,
    clock: Clock | None = None,
) -> SmokeReport:
    """Run one bounded contract check and return what it observed.

    Raises :class:`InputError` for an unusable URL or a film with nothing on sale,
    :class:`BfiContractError` for any drift, and lets the transport's challenge, circuit,
    and network errors through unchanged.
    """
    clock = clock if clock is not None else SystemClock()
    settings = Settings(
        telegram_bot_token="",
        allowed_user_ids=frozenset(),
        database_path=_UNUSED_DATABASE_PATH,
        bfi_impersonate_profile=profile,
    )
    # Built before the URL is validated so that every path out of this function -- including
    # a rejected URL -- runs the same `finally`. A session that is created and never closed
    # leaks a connection pool; there is no second cleanup path worth having.
    transport = BfiTransport(settings, _EphemeralCircuitStore(), clock, session=session)
    recorder = _RecordingTransport(transport)
    try:
        article = parse_article_url(film_url)
        # No caching: each document must genuinely be fetched, so the recorded counts are
        # the counts that reached BFI.
        gateway = BfiGateway(recorder, clock, cache_seconds=0.0)

        listing = await gateway.list_performances(article.slug)
        performance = _first_eligible(listing)
        seat_map = await gateway.load_seat_map(performance)

        recorder.require_all_ok()
        _require_complete_seats(seat_map)
        available = _count_available(seat_map)
        _require_exact_availability(performance, available)
        _require_bounded_reads(recorder)

        return SmokeReport(
            profile=profile,
            slug=article.slug,
            article_pages=recorder.count(DocumentKind.ARTICLE),
            seat_maps=recorder.count(DocumentKind.SEAT_MAP),
            performances=len(listing),
            eligible_performances=sum(1 for row in listing if _is_eligible(row)),
            performance_id=performance.performance_id,
            performance_start=performance.start,
            reported_availability=performance.availability_num,
            seats_parsed=len(seat_map.seats),
            available_seats=available,
            offerable_seats=sum(
                1 for seat in seat_map.seats if seat.status is SeatStatus.AVAILABLE
            ),
            price_zones=len({seat.zone.zone_id for seat in seat_map.seats if seat.zone}),
            seat_statuses=_status_histogram(seat_map),
            bytes_fetched=recorder.bytes_fetched,
        )
    finally:
        await transport.close()


def _is_eligible(performance: Performance) -> bool:
    return (
        is_on_sale(performance)
        and performance.reserved_seating
        and performance.availability_num > 0
    )


def _first_eligible(listing: PerformanceListing) -> Performance:
    for performance in listing:
        if _is_eligible(performance):
            return performance
    raise InputError(
        f"no on-sale reserved performance with availability among {len(listing)} parsed "
        "performance(s); the contract cannot be checked against this film right now"
    )


def _count_available(seat_map: SeatMap) -> int:
    return sum(1 for seat in seat_map.seats if seat.raw_status_code == AVAILABLE_STATUS_CODE)


def _status_histogram(seat_map: SeatMap) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = {}
    for seat in seat_map.seats:
        counts[seat.raw_status_code] = counts.get(seat.raw_status_code, 0) + 1
    return tuple(sorted(counts.items()))


def _require_complete_seats(seat_map: SeatMap) -> None:
    """Every seat must carry the fields a result is built from, and a known status.

    ``parse_seat_map`` already refuses a seat without a section, a seat number, or finite
    coordinates. The remaining two are checked here rather than there because the running
    service can still do useful work without them, and only this check should be strict:
    a row label is what a user reads off a result, a price zone is what a result is
    grouped by, and an unrecognised status code means BFI has introduced a seat state
    this parser silently treats as "not available".
    """
    for seat in seat_map.seats:
        if not seat.row:
            raise BfiContractError(f"seat {seat.seat_id!r}: missing data-seat-row")
        if seat.zone is None:
            raise BfiContractError(f"seat {seat.seat_id!r}: no price zone")
        if seat.status is SeatStatus.UNKNOWN:
            raise BfiContractError(
                f"seat {seat.seat_id!r}: unrecognised status code {seat.raw_status_code!r}"
            )


def _require_exact_availability(performance: Performance, available: int) -> None:
    if performance.availability_num != available:
        raise BfiContractError(
            f"availability mismatch for performance {performance.performance_id}: "
            f"listing reports {performance.availability_num}, "
            f'seat map has {available} seats with data-status="{AVAILABLE_STATUS_CODE}"'
        )


def _require_bounded_reads(recorder: _RecordingTransport) -> None:
    """Guard the bound itself: one seat map, and at least the film page."""
    seat_maps = recorder.count(DocumentKind.SEAT_MAP)
    if seat_maps != 1:
        raise BfiContractError(f"expected exactly one seat-map read, made {seat_maps}")
    articles = recorder.count(DocumentKind.ARTICLE)
    if articles < 1:
        raise BfiContractError("expected at least one article read, made none")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cinema-friend-smoke",
        description=(
            "Read one BFI film page, its pagination chain, and one seat map, and verify "
            "that the parsers still agree with what the site serves. Read-only: it never "
            "selects, holds, or reserves a seat, and never scans the programme."
        ),
    )
    parser.add_argument("film_url", metavar="FILM_URL", help="a BFI IMAX film page URL")
    parser.add_argument(
        "--profile",
        default="chrome",
        help="curl_cffi impersonation profile to use for every request (default: chrome)",
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    session: AsyncHttpSession | None = None,
    clock: Clock | None = None,
) -> int:
    args = _parse_args(argv)
    try:
        report = asyncio.run(
            run_smoke(args.film_url, profile=args.profile, session=session, clock=clock)
        )
    except InputError as error:
        return _fail(EXIT_INPUT, f"input error: {error}")
    except (BfiChallengeError, CircuitOpenError) as error:
        return _fail(EXIT_CHALLENGE, f"challenged: {error}")
    except BfiContractError as error:
        return _fail(EXIT_CONTRACT, f"contract mismatch: {error}")
    except OSError as error:
        # BfiNetworkError is an OSError, and so is curl_cffi's RequestException, so one
        # clause covers both the transport's own classification and anything the HTTP
        # client raises on its way out.
        return _fail(EXIT_NETWORK, f"network failure: {error}")

    print(report.render())
    return EXIT_OK


def _fail(code: int, message: str) -> int:
    print(message, file=sys.stderr)
    return code


if __name__ == "__main__":  # pragma: no cover - exercised as a process, not a test
    raise SystemExit(main())
