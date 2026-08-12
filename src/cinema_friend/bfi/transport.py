"""Chrome-impersonating BFI transport with spacing, retries, and a host circuit."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Final, Protocol, cast
from urllib.parse import urljoin, urlsplit

from curl_cffi.requests import AsyncSession

from cinema_friend.bfi.urls import describe_url, validate_redirect_target
from cinema_friend.clock import Clock
from cinema_friend.config import Settings
from cinema_friend.domain.errors import BfiChallengeError, BfiNetworkError, CircuitOpenError
from cinema_friend.domain.results import CircuitState, HostCircuit

# Redirect statuses we follow manually. 300 (Multiple Choices) and the
# not-modified/use-proxy family are deliberately excluded and fail closed.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Cloudflare's interactive JS/managed challenge is served with HTTP 200, so it
# must be detected from body content rather than status code alone.
#
# Every marker here must appear only in a body served *instead of* the page.
# `cdn-cgi/challenge-platform` on its own does not qualify: an ordinary,
# unchallenged BFI 200 embeds the platform's passive JSD probe at
# `/cdn-cgi/challenge-platform/scripts/jsd/main.js`, so matching the bare prefix
# classified every successful fetch as a challenge and tripped the host circuit
# on the first request. The `/h/` path is the managed/orchestrate challenge
# runtime, which is only ever fetched by an interstitial.
_INTERSTITIAL_MARKERS = (
    "Just a moment...",
    "cf-browser-verification",
    "/cdn-cgi/challenge-platform/h/",
    "Enable JavaScript and cookies to continue",
)

# Escalating re-probe delays once the host circuit trips, in step order.
_CIRCUIT_DELAYS: tuple[timedelta, ...] = (
    timedelta(minutes=15),
    timedelta(minutes=30),
    timedelta(hours=1),
    timedelta(hours=2),
    timedelta(hours=4),
    timedelta(hours=6),
)

# Server-error / network-exception retry ladder (seconds before each retry),
# each multiplied by an injected positive jitter factor.
_SERVER_ERROR_DELAYS: tuple[float, ...] = (1.0, 3.0)

# Unmarked-403 retry ladder (seconds before each retry); deterministic, no jitter.
_FORBIDDEN_DELAYS: tuple[float, ...] = (2.0, 4.0, 6.0)

_MAX_REDIRECT_HOPS = 5

CONNECT_TIMEOUT_SECONDS: Final = 10.0
"""How long one hop may spend establishing its connection."""

TOTAL_TIMEOUT_SECONDS: Final = 45.0
"""How long one hop may take end to end, connection included."""

REQUEST_TIMEOUT: Final = (CONNECT_TIMEOUT_SECONDS, TOTAL_TIMEOUT_SECONDS - CONNECT_TIMEOUT_SECONDS)
"""The explicit budget passed to every outbound GET, in ``curl_cffi``'s own units.

Verified against ``curl_cffi`` 0.16 (``requests/utils.py``): for a non-streamed request
a ``(connect, read)`` tuple sets ``CONNECTTIMEOUT_MS = connect`` and
``TIMEOUT_MS = connect + read``. The tuple below therefore *is* a 10-second connect and
a 45-second whole-request budget, which is what the design specifies.

It is passed per request rather than left to the session default (a bare 30 seconds,
with no separate connection bound) because the deadline a check runs under is part of
this transport's contract, not a property of whichever session it was handed: a session
supplied by the application, or by a test, would otherwise silently change it. Without
it, a hung socket parks a check forever, holds one of two concurrency slots, and keeps
the scheduler's shutdown grace period waiting on work that will never finish.
"""

# How long a persisted HALF_OPEN record is trusted as "someone is actively
# probing" before it is treated as abandoned (the process that claimed it
# died mid-probe) and made reclaimable again. Must comfortably exceed the
# worst-case bounded in-flight duration of a single probe: up to
# _MAX_REDIRECT_HOPS + 1 hops, each bounded by the slower retry ladder
# (2 + 4 + 6 = 12s for unmarked 403s), i.e. 6 * 12 = 72s worst case. 120s
# leaves comfortable margin while staying far shorter than the shortest
# circuit re-probe step (15 minutes), so a crashed prober is reclaimed long
# before the next scheduled probe would occur anyway.
_PROBE_LEASE_SECONDS = 120.0

# How many times a non-probing challenge writer re-reads and re-decides after losing a
# compare-and-swap. Each loss means a concurrent writer got there first, and re-reading
# usually resolves to "this incident is already recorded"; a small bound keeps a busy
# host from spinning here instead of returning the challenge to its caller.
_CAS_ATTEMPTS = 3


class DocumentKind(Enum):
    """What kind of BFI document a request is fetching."""

    ARTICLE = "article"
    SEAT_MAP = "seat_map"


@dataclass(frozen=True, slots=True)
class FetchedDocument:
    """A successfully retrieved BFI document."""

    url: str
    status_code: int
    headers: Mapping[str, str]
    text: str
    byte_count: int


class HttpResponse(Protocol):
    """Structural shape of the response objects returned by ``AsyncHttpSession.get``."""

    status_code: int
    headers: Mapping[str, str | None]
    text: str
    content: bytes
    url: str


class AsyncHttpSession(Protocol):
    """The subset of ``curl_cffi.requests.AsyncSession`` the transport needs."""

    async def get(self, url: str, **kwargs: object) -> HttpResponse: ...

    async def close(self) -> None: ...


class HostCircuitStore(Protocol):
    """Persists one :class:`HostCircuit` per host.

    ``save`` is unconditional and is for seeding or administrative writes. Every
    contested transition goes through ``compare_and_swap``, which lands only while the
    stored row still carries the revision the caller observed; that is what keeps two
    processes from both claiming a probe or replaying a resolved incident over a newer
    one, since neither one's locks are visible to the other.
    """

    async def load(self, host: str) -> HostCircuit: ...

    async def save(self, circuit: HostCircuit) -> None: ...

    async def compare_and_swap(self, circuit: HostCircuit) -> HostCircuit | None: ...


@dataclass(frozen=True, slots=True)
class _CircuitTicket:
    """What one request learned about the circuit before it went out.

    ``probing`` marks the caller as the single permitted prober. ``observed_generation``
    is the incident number it saw, used to recognise a challenge that belongs to an
    incident a sibling request already recorded. ``revision`` is the fencing token the
    caller's own write must still match: for a prober it is the revision its probe claim
    produced, so a claim that has since been superseded can no longer resolve the circuit.
    """

    probing: bool
    observed_generation: int
    revision: int


def _hostname(url: str) -> str:
    return urlsplit(url).hostname or ""


def _header(headers: Mapping[str, str | None], name: str) -> str | None:
    """Case-insensitive header lookup independent of the mapping implementation."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _looks_like_interstitial(text: str) -> bool:
    return any(marker in text for marker in _INTERSTITIAL_MARKERS)


class _Classification(Enum):
    SUCCESS = "success"
    REDIRECT = "redirect"
    CHALLENGE = "challenge"
    UNMARKED_403 = "unmarked_403"
    SERVER_ERROR = "server_error"
    CLIENT_ERROR = "client_error"


def _classify(response: HttpResponse) -> _Classification:
    if _header(response.headers, "cf-mitigated"):
        return _Classification.CHALLENGE
    if response.status_code == 429:
        return _Classification.CHALLENGE
    if response.status_code in _REDIRECT_STATUSES:
        return _Classification.REDIRECT
    if response.status_code == 403:
        return _Classification.UNMARKED_403
    if 500 <= response.status_code < 600:
        return _Classification.SERVER_ERROR
    if 200 <= response.status_code < 300:
        if _looks_like_interstitial(response.text):
            return _Classification.CHALLENGE
        return _Classification.SUCCESS
    return _Classification.CLIENT_ERROR


class BfiTransport:
    """Shared Chrome-impersonating session with spacing, retries, and a circuit breaker.

    Concurrency model:

    - ``_semaphore`` bounds simultaneous in-flight requests to
      ``settings.bfi_max_concurrency``.
    - ``_spacing_lock`` serializes only the *decision* of when the next request
      may start (so request starts are spaced by at least
      ``settings.bfi_min_spacing_seconds``); the outbound call itself runs
      outside the lock so two permitted requests can overlap in flight.
    - ``_probe_lock`` serializes the transition that claims the single
      permitted probe of an elapsed-but-still-open circuit, or of a stale
      (lease-expired) ``HALF_OPEN`` record left behind by a process that
      died mid-probe. It is held only long enough to read-check-write the
      circuit state (never for the duration of the outbound probe request),
      so a rejected concurrent caller fails fast with
      :class:`CircuitOpenError` instead of queueing to become a second
      prober.
    - ``_advance_lock`` serializes the read-modify-write that records a
      challenge against the circuit, so two requests racing against the
      same ``CLOSED`` circuit generation coalesce onto exactly one fresh
      ``OPEN`` transition instead of the second one mistaking the first's
      own transition for a later, separate incident and double-advancing
      the backoff step.

    Both locks only order callers *inside* one transport instance. Two
    processes -- or two transports sharing a database -- see none of each
    other's locks, so every circuit transition is additionally written
    through ``HostCircuitStore.compare_and_swap`` against the revision the
    caller observed. A writer whose view has been superseded loses the swap
    and drops its write rather than overwriting the newer state.
    """

    def __init__(
        self,
        settings: Settings,
        circuit_store: HostCircuitStore,
        clock: Clock,
        session: AsyncHttpSession | None = None,
        jitter_source: Callable[[], float] | None = None,
    ) -> None:
        self.circuit_store = circuit_store
        self._clock = clock
        # `AsyncSession`'s TypedDict-unpacked keyword signature doesn't structurally
        # match our simplified `AsyncHttpSession` protocol, though it satisfies the
        # two methods (`get`, `close`) we actually call at runtime.
        self._session: AsyncHttpSession = session if session is not None else cast(
            "AsyncHttpSession", AsyncSession(impersonate=settings.bfi_impersonate_profile)
        )
        self._semaphore = asyncio.Semaphore(settings.bfi_max_concurrency)
        self._spacing_lock = asyncio.Lock()
        self._probe_lock = asyncio.Lock()
        self._advance_lock = asyncio.Lock()
        self._min_spacing = settings.bfi_min_spacing_seconds
        self._last_start_monotonic: float | None = None
        self._jitter: Callable[[], float] = jitter_source or (lambda: random.uniform(0.0, 0.2))

    async def close(self) -> None:
        await self._session.close()

    async def get(self, url: str, kind: DocumentKind) -> FetchedDocument:
        host = _hostname(url)
        ticket = await self._begin_circuit_check(host)
        try:
            document = await self._fetch_following_redirects(url, kind)
        except BfiChallengeError:
            await self._advance_circuit_after_challenge(host, ticket)
            raise
        except BaseException:
            if ticket.probing:
                await self._advance_circuit_after_challenge(host, ticket)
            raise
        else:
            if ticket.probing:
                await self._close_circuit(host, ticket)
            return document

    # -- circuit breaker -----------------------------------------------

    async def _begin_circuit_check(self, host: str) -> _CircuitTicket:
        """Enforce the persisted circuit and describe what this caller may do.

        Returns the :class:`_CircuitTicket` that the caller threads into whichever
        transition its outcome triggers, or raises :class:`CircuitOpenError` when no
        request is permitted at all.
        """
        circuit = await self.circuit_store.load(host)
        if circuit.state is CircuitState.CLOSED:
            return _CircuitTicket(
                probing=False,
                observed_generation=circuit.generation,
                revision=circuit.revision,
            )
        now = self._clock.now()
        if circuit.state is CircuitState.HALF_OPEN:
            if (now - circuit.updated_at).total_seconds() < _PROBE_LEASE_SECONDS:
                raise CircuitOpenError(f"circuit for {host} is already being probed")
            # The persisted HALF_OPEN lease has expired without ever being
            # resolved to CLOSED or OPEN -- the process that claimed it most
            # likely died mid-probe. Reclaim it exactly like an elapsed OPEN
            # circuit rather than leaving the host permanently blocked.
            return await self._claim_probe(host, now)
        if circuit.next_probe is None or now < circuit.next_probe:
            raise CircuitOpenError(f"circuit for {host} is open until {circuit.next_probe}")
        return await self._claim_probe(host, now)

    async def _claim_probe(self, host: str, now: datetime) -> _CircuitTicket:
        async with self._probe_lock:
            # Re-read after acquiring the lock: another caller may have
            # already claimed the probe (or the window may have moved) while
            # we were waiting.
            circuit = await self.circuit_store.load(host)
            elapsed_open = (
                circuit.state is CircuitState.OPEN
                and circuit.next_probe is not None
                and now >= circuit.next_probe
            )
            stale_half_open = (
                circuit.state is CircuitState.HALF_OPEN
                and (now - circuit.updated_at).total_seconds() >= _PROBE_LEASE_SECONDS
            )
            if not (elapsed_open or stale_half_open):
                raise CircuitOpenError(f"circuit for {host} is open")
            claimed = await self.circuit_store.compare_and_swap(
                HostCircuit(
                    host=host,
                    state=CircuitState.HALF_OPEN,
                    backoff_step=circuit.backoff_step,
                    generation=circuit.generation,
                    next_probe=circuit.next_probe,
                    updated_at=now,
                    revision=circuit.revision,
                )
            )
            if claimed is None:
                # Another process claimed the same probe slot between our read
                # and our write. Exactly one prober is permitted, so this caller
                # is refused rather than issuing a second probe request.
                raise CircuitOpenError(f"circuit for {host} is already being probed")
            return _CircuitTicket(
                probing=True,
                observed_generation=circuit.generation,
                revision=claimed.revision,
            )

    async def _advance_circuit_after_challenge(self, host: str, ticket: _CircuitTicket) -> None:
        """Record a challenge against the circuit, opening or advancing its backoff.

        A prober gets exactly one fenced attempt against the revision its claim produced:
        losing that swap means its lease was already superseded, and replaying a resolved
        probe over the newer incident would corrupt the backoff ladder. A non-probing
        caller instead re-reads and re-decides, because a lost swap there only means a
        sibling wrote first -- and the incident comparison then recognises that write as
        the same trip and stops.
        """
        async with self._advance_lock:
            for _ in range(1 if ticket.probing else _CAS_ATTEMPTS):
                circuit = await self.circuit_store.load(host)
                now = self._clock.now()
                if not ticket.probing and circuit.state is CircuitState.CLOSED:
                    # Fresh trip: the first challenge to observe CLOSED wins.
                    backoff_step = 0
                    generation = circuit.generation + 1
                elif not ticket.probing and circuit.generation == ticket.observed_generation + 1:
                    # A concurrent sibling request observed the same CLOSED
                    # generation we did and has already recorded the fresh
                    # OPEN transition for this incident. Don't double-advance
                    # the backoff step for a second signal from the same trip.
                    return
                else:
                    backoff_step = min(circuit.backoff_step + 1, len(_CIRCUIT_DELAYS) - 1)
                    generation = circuit.generation
                written = await self.circuit_store.compare_and_swap(
                    HostCircuit(
                        host=host,
                        state=CircuitState.OPEN,
                        backoff_step=backoff_step,
                        generation=generation,
                        next_probe=now + _CIRCUIT_DELAYS[backoff_step],
                        updated_at=now,
                        revision=ticket.revision if ticket.probing else circuit.revision,
                    )
                )
                if written is not None:
                    return

    async def _close_circuit(self, host: str, ticket: _CircuitTicket) -> None:
        """Close the circuit after a successful probe, unless the claim was superseded."""
        circuit = await self.circuit_store.load(host)
        await self.circuit_store.compare_and_swap(
            HostCircuit(
                host=host,
                state=CircuitState.CLOSED,
                backoff_step=0,
                generation=circuit.generation,
                next_probe=None,
                updated_at=self._clock.now(),
                revision=ticket.revision,
            )
        )

    # -- redirects --------------------------------------------------------

    async def _fetch_following_redirects(self, url: str, kind: DocumentKind) -> FetchedDocument:
        current_url = url
        for hop in range(_MAX_REDIRECT_HOPS + 1):
            response = await self._request_hop(current_url, kind)
            if _classify(response) is not _Classification.REDIRECT:
                return _build_document(current_url, response)
            if hop == _MAX_REDIRECT_HOPS:
                break
            location = _header(response.headers, "location")
            if not location:
                raise BfiNetworkError(
                    f"redirect from {describe_url(current_url)} ({kind.value}) "
                    "is missing a Location header"
                )
            target = urljoin(current_url, location)
            # validate_redirect_target() is a strict gate (Task 2, article
            # routes only) -- it raises on a disallowed target but its
            # return value is a rewritten canonical URL, not the requested
            # one. Discard that return value and request the actual
            # validated absolute Location instead, so redirect query state
            # (e.g. a continuation token) survives and a chain of distinct
            # default.asp targets doesn't collapse onto one repeated URL.
            validate_redirect_target(target)
            current_url = target
        raise BfiNetworkError(f"too many redirects starting from {describe_url(url)} ({kind.value})")

    # -- single hop with bounded retries -----------------------------------

    async def _request_hop(self, url: str, kind: DocumentKind) -> HttpResponse:
        server_attempt = 0
        forbidden_attempt = 0
        while True:
            try:
                response = await self._throttled_request(url)
            except OSError as exc:
                if server_attempt >= len(_SERVER_ERROR_DELAYS):
                    raise BfiNetworkError(
                        f"network error contacting {describe_url(url)} ({kind.value})"
                    ) from exc
                await self._clock.sleep(_SERVER_ERROR_DELAYS[server_attempt] * (1.0 + self._jitter()))
                server_attempt += 1
                continue

            classification = _classify(response)
            if classification in (_Classification.SUCCESS, _Classification.REDIRECT):
                return response
            if classification is _Classification.CHALLENGE:
                raise BfiChallengeError(
                    f"BFI challenged the request to {describe_url(url)} ({kind.value})"
                )
            if classification is _Classification.SERVER_ERROR:
                if server_attempt >= len(_SERVER_ERROR_DELAYS):
                    raise BfiNetworkError(
                        f"server error {response.status_code} from {describe_url(url)} ({kind.value})"
                    )
                await self._clock.sleep(_SERVER_ERROR_DELAYS[server_attempt] * (1.0 + self._jitter()))
                server_attempt += 1
                continue
            if classification is _Classification.UNMARKED_403:
                if forbidden_attempt >= len(_FORBIDDEN_DELAYS):
                    raise BfiChallengeError(f"persistent 403 from {describe_url(url)} ({kind.value})")
                await self._clock.sleep(_FORBIDDEN_DELAYS[forbidden_attempt])
                forbidden_attempt += 1
                continue
            raise BfiNetworkError(
                f"unexpected status {response.status_code} from "
                f"{describe_url(url)} ({kind.value})"
            )

    # -- concurrency + spacing ----------------------------------------------

    async def _throttled_request(self, url: str) -> HttpResponse:
        async with self._semaphore:
            async with self._spacing_lock:
                now = self._clock.monotonic()
                if self._last_start_monotonic is not None:
                    elapsed = now - self._last_start_monotonic
                    if elapsed < self._min_spacing:
                        await self._clock.sleep(self._min_spacing - elapsed)
                self._last_start_monotonic = self._clock.monotonic()
            return await self._session.get(url, allow_redirects=False, timeout=REQUEST_TIMEOUT)


def _build_document(url: str, response: HttpResponse) -> FetchedDocument:
    headers = {key: value for key, value in response.headers.items() if value is not None}
    return FetchedDocument(
        url=url,
        status_code=response.status_code,
        headers=headers,
        text=response.text,
        byte_count=len(response.content),
    )
