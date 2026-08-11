"""Tests for cinema_friend.bfi.transport."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cinema_friend.bfi.transport import BfiTransport, DocumentKind, FetchedDocument
from cinema_friend.bfi.urls import film_page_url, seat_map_url
from cinema_friend.config import Settings
from cinema_friend.domain.errors import BfiChallengeError, BfiNetworkError, CircuitOpenError
from cinema_friend.domain.results import CircuitState
from tests.fakes import (
    FakeClock,
    FakeNetworkError,
    FakeSession,
    MemoryCircuitStore,
    open_circuit,
    response,
)

BFI_HOST = "whatson.bfi.org.uk"
FILM_URL = film_page_url("dog-stars")
SEAT_MAP_URL = seat_map_url("2152D1E8-CFF7-419F-BE57-F51C1E490F24")
ARTICLE = "<html><body>dog stars article</body></html>"
INTERSTITIAL = "<html><head><title>Just a moment...</title></head><body>cf-browser-verification</body></html>"


def make_settings() -> Settings:
    return Settings(
        telegram_bot_token="token",
        allowed_user_ids=frozenset({1}),
        database_path=Path("unused.db"),  # never opened by the transport under test
    )


def make_transport(
    *,
    session: FakeSession | None = None,
    circuit_store: MemoryCircuitStore | None = None,
    clock: FakeClock | None = None,
    jitter_source: object = None,
) -> BfiTransport:
    return BfiTransport(
        make_settings(),
        circuit_store if circuit_store is not None else MemoryCircuitStore(),
        clock if clock is not None else FakeClock(datetime(2026, 1, 1, tzinfo=UTC)),
        session=session if session is not None else FakeSession([]),
        jitter_source=jitter_source,
    )


# ---------------------------------------------------------------------------
# Challenge classification
# ---------------------------------------------------------------------------


async def test_cf_mitigated_403_opens_circuit_without_immediate_retry():
    session = FakeSession([response(403, headers={"cf-mitigated": "challenge"})])
    transport = make_transport(session=session)
    with pytest.raises(BfiChallengeError):
        await transport.get(FILM_URL, DocumentKind.ARTICLE)
    assert len(session.calls) == 1
    assert (await transport.circuit_store.load(BFI_HOST)).state is CircuitState.OPEN


async def test_429_opens_circuit_without_immediate_retry():
    session = FakeSession([response(429)])
    transport = make_transport(session=session)
    with pytest.raises(BfiChallengeError):
        await transport.get(FILM_URL, DocumentKind.ARTICLE)
    assert len(session.calls) == 1


async def test_interstitial_200_opens_circuit_without_immediate_retry():
    session = FakeSession([response(200, INTERSTITIAL)])
    transport = make_transport(session=session)
    with pytest.raises(BfiChallengeError):
        await transport.get(FILM_URL, DocumentKind.ARTICLE)
    assert len(session.calls) == 1


async def test_challenge_sets_next_probe_fifteen_minutes_out():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    session = FakeSession([response(429)])
    transport = make_transport(session=session, clock=clock)
    with pytest.raises(BfiChallengeError):
        await transport.get(FILM_URL, DocumentKind.ARTICLE)
    circuit = await transport.circuit_store.load(BFI_HOST)
    assert circuit.next_probe == clock.now() + timedelta(minutes=15)
    assert circuit.backoff_step == 0


# ---------------------------------------------------------------------------
# Unmarked 403 retry-then-escalate
# ---------------------------------------------------------------------------


async def test_transient_403_retries_two_four_six_seconds():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    session = FakeSession(
        [response(403), response(403), response(403), response(200, ARTICLE)]
    )
    document = await make_transport(session=session, clock=clock).get(FILM_URL, DocumentKind.ARTICLE)
    assert document.status_code == 200
    assert clock.sleeps == [2.0, 4.0, 6.0]


async def test_persistent_unmarked_403_escalates_to_challenge():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    session = FakeSession([response(403), response(403), response(403), response(403)])
    transport = make_transport(session=session, clock=clock)
    with pytest.raises(BfiChallengeError):
        await transport.get(FILM_URL, DocumentKind.ARTICLE)
    assert clock.sleeps == [2.0, 4.0, 6.0]
    assert len(session.calls) == 4
    assert (await transport.circuit_store.load(BFI_HOST)).state is CircuitState.OPEN


# ---------------------------------------------------------------------------
# 5xx / network exception retries with jitter
# ---------------------------------------------------------------------------


async def test_5xx_retries_one_and_three_seconds_with_jitter():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    session = FakeSession([response(503), response(502), response(200, ARTICLE)])
    document = await make_transport(
        session=session, clock=clock, jitter_source=lambda: 0.1
    ).get(FILM_URL, DocumentKind.ARTICLE)
    assert document.status_code == 200
    assert clock.sleeps == pytest.approx([1.1, 3.3])


async def test_network_exception_retries_and_then_raises_bfi_network_error():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    session = FakeSession([FakeNetworkError("boom"), FakeNetworkError("boom"), FakeNetworkError("boom")])
    transport = make_transport(session=session, clock=clock, jitter_source=lambda: 0.0)
    with pytest.raises(BfiNetworkError):
        await transport.get(FILM_URL, DocumentKind.ARTICLE)
    assert clock.sleeps == [1.0, 3.0]
    assert len(session.calls) == 3
    # Non-challenge failures never touch the circuit.
    assert (await transport.circuit_store.load(BFI_HOST)).state is CircuitState.CLOSED


async def test_mixed_network_exception_and_5xx_share_one_retry_ladder():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    session = FakeSession([FakeNetworkError("boom"), response(500), response(200, ARTICLE)])
    document = await make_transport(
        session=session, clock=clock, jitter_source=lambda: 0.0
    ).get(FILM_URL, DocumentKind.ARTICLE)
    assert document.status_code == 200
    assert clock.sleeps == [1.0, 3.0]


# ---------------------------------------------------------------------------
# Other 4xx: no retry
# ---------------------------------------------------------------------------


async def test_other_4xx_raises_without_retry():
    session = FakeSession([response(404)])
    transport = make_transport(session=session)
    with pytest.raises(BfiNetworkError):
        await transport.get(FILM_URL, DocumentKind.ARTICLE)
    assert len(session.calls) == 1


# ---------------------------------------------------------------------------
# Circuit breaker: open / probe
# ---------------------------------------------------------------------------


async def test_open_circuit_blocks_until_probe_time():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = MemoryCircuitStore(open_circuit(clock.now() + timedelta(minutes=15), host=BFI_HOST))
    with pytest.raises(CircuitOpenError):
        await make_transport(circuit_store=store, clock=clock).get(FILM_URL, DocumentKind.ARTICLE)


async def test_elapsed_open_circuit_allows_single_probe_and_closes_on_success():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = MemoryCircuitStore(
        open_circuit(clock.now() - timedelta(seconds=1), host=BFI_HOST, backoff_step=2)
    )
    session = FakeSession([response(200, ARTICLE)])
    document = await make_transport(session=session, circuit_store=store, clock=clock).get(
        FILM_URL, DocumentKind.ARTICLE
    )
    assert document.status_code == 200
    circuit = await store.load(BFI_HOST)
    assert circuit.state is CircuitState.CLOSED
    assert circuit.backoff_step == 0
    assert circuit.next_probe is None


async def test_elapsed_open_circuit_probe_failure_advances_backoff():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = MemoryCircuitStore(
        open_circuit(clock.now() - timedelta(seconds=1), host=BFI_HOST, backoff_step=0, generation=1)
    )
    session = FakeSession([response(429)])
    transport = make_transport(session=session, circuit_store=store, clock=clock)
    with pytest.raises(BfiChallengeError):
        await transport.get(FILM_URL, DocumentKind.ARTICLE)
    circuit = await store.load(BFI_HOST)
    assert circuit.state is CircuitState.OPEN
    assert circuit.backoff_step == 1
    assert circuit.generation == 1
    assert circuit.next_probe == clock.now() + timedelta(minutes=30)


async def test_probe_backoff_caps_at_six_hours():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = MemoryCircuitStore(
        open_circuit(clock.now() - timedelta(seconds=1), host=BFI_HOST, backoff_step=5)
    )
    session = FakeSession([response(429)])
    transport = make_transport(session=session, circuit_store=store, clock=clock)
    with pytest.raises(BfiChallengeError):
        await transport.get(FILM_URL, DocumentKind.ARTICLE)
    circuit = await store.load(BFI_HOST)
    assert circuit.backoff_step == 5
    assert circuit.next_probe == clock.now() + timedelta(hours=6)


async def test_only_one_concurrent_probe_is_allowed():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = MemoryCircuitStore(
        open_circuit(clock.now() - timedelta(seconds=1), host=BFI_HOST, backoff_step=0)
    )
    session = FakeSession([response(200, ARTICLE)])
    transport = make_transport(session=session, circuit_store=store, clock=clock)

    results = await asyncio.gather(
        transport.get(FILM_URL, DocumentKind.ARTICLE),
        transport.get(FILM_URL, DocumentKind.ARTICLE),
        return_exceptions=True,
    )
    outcomes = sorted(type(item).__name__ for item in results)
    assert outcomes == ["CircuitOpenError", "FetchedDocument"]
    assert len(session.calls) == 1


async def test_probe_failure_does_not_leave_circuit_stuck_half_open():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = MemoryCircuitStore(
        open_circuit(clock.now() - timedelta(seconds=1), host=BFI_HOST, backoff_step=0)
    )
    session = FakeSession([FakeNetworkError("boom"), FakeNetworkError("boom"), FakeNetworkError("boom")])
    transport = make_transport(session=session, circuit_store=store, clock=clock, jitter_source=lambda: 0.0)
    with pytest.raises(BfiNetworkError):
        await transport.get(FILM_URL, DocumentKind.ARTICLE)
    circuit = await store.load(BFI_HOST)
    assert circuit.state is CircuitState.OPEN
    assert circuit.backoff_step == 1


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------


async def test_follows_valid_redirect_to_article_target():
    redirected_to = "https://whatson.bfi.org.uk/imax/Online/article/dog-stars"
    session = FakeSession(
        [
            response(302, headers={"location": redirected_to}),
            response(200, ARTICLE),
        ]
    )
    document = await make_transport(session=session).get(FILM_URL, DocumentKind.ARTICLE)
    assert document.status_code == 200
    assert document.url == FILM_URL
    assert session.calls == [FILM_URL, FILM_URL]


async def test_redirect_to_disallowed_target_is_rejected():
    disallowed = (
        "https://whatson.bfi.org.uk/imax/Online/mapSelect.asp"
        "?BOparam::WSmap::loadMap::performance_ids=2152D1E8-CFF7-419F-BE57-F51C1E490F24"
    )
    session = FakeSession([response(302, headers={"location": disallowed})])
    transport = make_transport(session=session)
    with pytest.raises(Exception, match="redirect target must stay on an allowed BFI route"):
        await transport.get(SEAT_MAP_URL, DocumentKind.SEAT_MAP)
    assert len(session.calls) == 1


async def test_redirect_missing_location_header_raises_network_error():
    session = FakeSession([response(302)])
    transport = make_transport(session=session)
    with pytest.raises(BfiNetworkError):
        await transport.get(FILM_URL, DocumentKind.ARTICLE)


async def test_more_than_five_redirect_hops_raises_network_error():
    same_target = FILM_URL
    session = FakeSession([response(302, headers={"location": same_target}) for _ in range(6)])
    transport = make_transport(session=session)
    with pytest.raises(BfiNetworkError, match="redirect"):
        await transport.get(FILM_URL, DocumentKind.ARTICLE)
    assert len(session.calls) == 6


# ---------------------------------------------------------------------------
# Concurrency and spacing
# ---------------------------------------------------------------------------


async def test_two_calls_run_concurrently_up_to_configured_limit():
    session = FakeSession([response(200, ARTICLE), response(200, ARTICLE)])
    transport = make_transport(session=session)
    await asyncio.gather(
        transport.get(FILM_URL, DocumentKind.ARTICLE),
        transport.get(SEAT_MAP_URL, DocumentKind.SEAT_MAP),
    )
    assert session.max_concurrent == 2


async def test_request_starts_are_spaced_by_at_least_one_second():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    session = FakeSession([response(200, ARTICLE), response(200, ARTICLE)])
    transport = make_transport(session=session, clock=clock)
    await asyncio.gather(
        transport.get(FILM_URL, DocumentKind.ARTICLE),
        transport.get(SEAT_MAP_URL, DocumentKind.SEAT_MAP),
    )
    assert clock.sleeps == [1.0]


# ---------------------------------------------------------------------------
# FetchedDocument shape and close()
# ---------------------------------------------------------------------------


async def test_get_returns_fetched_document_with_byte_count():
    session = FakeSession([response(200, ARTICLE)])
    document = await make_transport(session=session).get(FILM_URL, DocumentKind.ARTICLE)
    assert isinstance(document, FetchedDocument)
    assert document.byte_count == len(ARTICLE.encode("utf-8"))
    assert document.text == ARTICLE


async def test_close_closes_the_underlying_session():
    session = FakeSession([])
    transport = make_transport(session=session)
    await transport.close()
    assert session.closed is True
