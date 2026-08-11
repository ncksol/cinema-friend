"""Deterministic fakes shared by BFI transport (and later gateway) tests."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from cinema_friend.bfi.transport import DocumentKind, FetchedDocument
from cinema_friend.domain.results import CircuitState, HostCircuit

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class FakeNetworkError(OSError):
    """Simulated transport-level failure raised by :class:`FakeSession`."""


class FakeClock:
    """Deterministic clock: advances only when ``sleep`` is awaited."""

    def __init__(self, now: datetime) -> None:
        self.current = now
        self.monotonic_value = 0.0
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.monotonic_value

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.monotonic_value += seconds
        self.current += timedelta(seconds=seconds)


@dataclass
class FakeResponse:
    """Minimal stand-in for a ``curl_cffi`` response."""

    status_code: int
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    url: str = ""
    content: bytes = b""

    def __post_init__(self) -> None:
        if not self.content:
            self.content = self.text.encode("utf-8")


def response(
    status_code: int,
    body: str = "",
    *,
    headers: Mapping[str, str] | None = None,
    url: str = "",
) -> FakeResponse:
    """Build a :class:`FakeResponse` for queueing into :class:`FakeSession`."""
    return FakeResponse(status_code=status_code, text=body, headers=dict(headers or {}), url=url)


QueuedItem = FakeResponse | BaseException


class FakeSession:
    """Queues responses/exceptions, recording every requested URL.

    Also tracks the maximum number of ``get`` calls that were simultaneously
    in flight, so tests can assert on concurrency limits.
    """

    def __init__(self, items: Sequence[QueuedItem]) -> None:
        self._items: list[QueuedItem] = list(items)
        self.calls: list[str] = []
        self.max_concurrent = 0
        self._concurrent = 0
        self.closed = False

    async def get(self, url: str, **_kwargs: object) -> FakeResponse:
        self.calls.append(url)
        self._concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self._concurrent)
        try:
            # Yield control so concurrently scheduled callers can overlap,
            # making `max_concurrent` observable in concurrency tests.
            await asyncio.sleep(0)
            if not self._items:
                raise AssertionError(f"FakeSession has no queued response for {url}")
            item = self._items.pop(0)
            await asyncio.sleep(0)
            if isinstance(item, BaseException):
                raise item
            return item
        finally:
            self._concurrent -= 1

    async def close(self) -> None:
        self.closed = True


class MemoryCircuitStore:
    """In-memory :class:`HostCircuitStore`, persisting one circuit per host.

    Mirrors the SQLite store's revision semantics so transport behaviour proven here
    holds against the real store: an absent circuit reads as revision 0, and every
    persisted write advances the revision by one.
    """

    def __init__(self, initial: HostCircuit | None = None) -> None:
        self._circuits: dict[str, HostCircuit] = {}
        if initial is not None:
            self._circuits[initial.host] = _with_revision(initial, 1)

    async def load(self, host: str) -> HostCircuit:
        circuit = self._circuits.get(host)
        if circuit is None:
            return HostCircuit(
                host=host,
                state=CircuitState.CLOSED,
                backoff_step=0,
                generation=0,
                next_probe=None,
                updated_at=_EPOCH,
                revision=0,
            )
        return circuit

    async def save(self, circuit: HostCircuit) -> None:
        existing = self._circuits.get(circuit.host)
        revision = 1 if existing is None else existing.revision + 1
        self._circuits[circuit.host] = _with_revision(circuit, revision)

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


def fetched_document(text: str, *, url: str = "", status_code: int = 200) -> FetchedDocument:
    """Build a :class:`FetchedDocument` for queueing into :class:`FakeTransport`."""
    return FetchedDocument(
        url=url,
        status_code=status_code,
        headers={},
        text=text,
        byte_count=len(text.encode("utf-8")),
    )


QueuedDocument = FetchedDocument | BaseException


class FakeTransport:
    """Records requested URLs and returns queued documents/exceptions per URL.

    Values passed to ``responses`` may be a single item (returned on every
    call for that URL) or a ``list`` of items consumed in order, with the
    last item repeating once the list is exhausted.
    """

    def __init__(
        self, responses: Mapping[str, QueuedDocument | list[QueuedDocument]] | None = None
    ) -> None:
        self._queues: dict[str, list[QueuedDocument]] = {
            url: list(value) if isinstance(value, list) else [value]
            for url, value in (responses or {}).items()
        }
        self.calls: list[str] = []

    async def get(self, url: str, kind: DocumentKind) -> FetchedDocument:
        self.calls.append(url)
        queue = self._queues.get(url)
        if not queue:
            raise AssertionError(f"FakeTransport has no queued response for {url}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def call_count(self, url: str) -> int:
        return self.calls.count(url)


def open_circuit(
    next_probe_at: datetime,
    *,
    host: str,
    backoff_step: int = 0,
    generation: int = 1,
    updated_at: datetime | None = None,
) -> HostCircuit:
    """Build an already-OPEN :class:`HostCircuit` for test setup."""
    return HostCircuit(
        host=host,
        state=CircuitState.OPEN,
        backoff_step=backoff_step,
        generation=generation,
        next_probe=next_probe_at,
        updated_at=updated_at or next_probe_at,
    )
