"""Tests for cinema_friend.smoke: the one bounded, read-only BFI contract check.

The smoke command exists to answer one question against the live site -- "do the two
parsers still agree with what BFI actually serves?" -- and it has to answer it while
touching the site as little as a person opening one film page would. So the properties
under test are as much about restraint as about correctness:

- Bounded: exactly the film page, exactly its pagination chain, and exactly one seat
  map. The aggregate programme is never requested, and a second performance is never
  looked at once a first eligible one is found.
- Exact: BFI's own ``availability_num`` must equal the number of ``data-status="A"``
  seats the seat-map parser found. Drift in either parser shows up here as inequality,
  which is the whole reason to run it.
- Quiet: the report names the impersonation profile and counts, and carries neither the
  transient ``sToken`` nor any part of a fetched document.
- Honest about failure: each failure mode gets its own exit code, and the HTTP session
  is closed on every path out.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from cinema_friend.bfi.article_context import performance_from_row
from cinema_friend.bfi.urls import film_page_url, pagination_url, seat_map_url
from cinema_friend.domain.errors import BfiContractError, InputError
from cinema_friend.smoke import (
    EXIT_CHALLENGE,
    EXIT_CONTRACT,
    EXIT_INPUT,
    EXIT_NETWORK,
    EXIT_OK,
    main,
    run_smoke,
)
from tests.factories.bfi_html import (
    PERFORMANCE_ID,
    available_circles,
    make_article_html,
    performance_row,
    real_article_html,
    seat_map_html,
)
from tests.fakes import FakeClock, FakeNetworkError, FakeSession, response
from tests.fixtures import article_context_row_mappings

SLUG = "dog-stars"
FILM_URL = (
    "https://whatson.bfi.org.uk/imax/Online/default.asp"
    "?BOparam::WScontent::loadArticle::permalink=dog-stars"
)
ARTICLE_ID = "2152D1E8-CFF7-419F-BE57-F51C1E490F24"
TOKEN = "SMOKE-TOKEN-DO-NOT-PRINT"
OTHER_PERFORMANCE_ID = "9E1C4A70-1F2B-4C3D-8E5F-A0B1C2D3E4F5"


def clock() -> FakeClock:
    return FakeClock(datetime(2026, 8, 12, 9, 0, tzinfo=UTC))


def eligible_row(**overrides: Any) -> list[Any]:
    """A row the smoke must accept: on sale, reserved seating, one seat available.

    ``availability_num`` is 2 because the shared seat-map fixture carries two
    ``data-status="A"`` circles -- an ordinary seat and one accessible space -- and the
    contract the smoke enforces is equality against the raw attribute, which is what
    BFI's own counter counts.
    """
    return performance_row(**{"sales_status": "S", "availability_num": "2", **overrides})


def article_html(
    rows: list[list[Any]] | None = None,
    *,
    current_page: int = 1,
    total_pages: int = 1,
) -> str:
    return make_article_html(
        rows=rows if rows is not None else [eligible_row()],
        current_page=current_page,
        total_pages=total_pages,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )


def passing_session() -> FakeSession:
    """One single-page listing plus its seat map: the smallest passing run."""
    return FakeSession(
        [
            response(200, article_html()),
            response(200, seat_map_html()),
        ]
    )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


async def test_rejects_a_url_that_is_not_a_bfi_article() -> None:
    session = FakeSession([])

    with pytest.raises(InputError):
        await run_smoke(
            "https://example.com/imax/Online/default.asp?x=1",
            profile="chrome",
            session=session,
            clock=clock(),
        )

    assert session.calls == [], "an invalid URL must be rejected before any request"


async def test_rejects_a_bfi_url_without_a_film_permalink() -> None:
    session = FakeSession([])

    with pytest.raises(InputError):
        await run_smoke(
            "https://whatson.bfi.org.uk/imax/Online/default.asp",
            profile="chrome",
            session=session,
            clock=clock(),
        )

    assert session.calls == []


def test_invalid_input_exits_2() -> None:
    assert (
        main(
            ["https://example.com/nope"],
            session=FakeSession([]),
            clock=clock(),
        )
        == EXIT_INPUT
    )


# ---------------------------------------------------------------------------
# Bounded traversal
# ---------------------------------------------------------------------------


async def test_fetches_page_one_every_pagination_page_and_exactly_one_seat_map() -> None:
    session = FakeSession(
        [
            response(200, article_html([eligible_row()], current_page=1, total_pages=3)),
            response(
                200,
                article_html(
                    [performance_row(id=OTHER_PERFORMANCE_ID, sales_status="C")],
                    current_page=2,
                    total_pages=3,
                ),
            ),
            response(
                200,
                article_html(
                    [
                        performance_row(
                            id="11111111-2222-3333-4444-555555555555",
                            sales_status="C",
                        )
                    ],
                    current_page=3,
                    total_pages=3,
                ),
            ),
            response(200, seat_map_html()),
        ]
    )

    report = await run_smoke(FILM_URL, profile="chrome", session=session, clock=clock())

    assert session.calls == [
        film_page_url(SLUG),
        pagination_url(TOKEN, 2, ARTICLE_ID),
        pagination_url(TOKEN, 3, ARTICLE_ID),
        seat_map_url(PERFORMANCE_ID),
    ], "the smoke must read the film page, its pagination chain, and one seat map -- nothing else"
    assert report.article_pages == 3
    assert report.seat_maps == 1
    assert report.performances == 3


async def test_chooses_the_first_on_sale_reserved_performance_with_availability() -> None:
    session = FakeSession(
        [
            response(
                200,
                article_html(
                    [
                        performance_row(
                            id="00000000-0000-4000-8000-000000000001",
                            sales_status="C",
                            availability_num="200",
                        ),
                        performance_row(
                            id="00000000-0000-4000-8000-000000000002",
                            sales_status="S",
                            availability_num="0",
                        ),
                        performance_row(
                            id="00000000-0000-4000-8000-000000000003",
                            sales_status="S",
                            availability_num="5",
                            options=[],
                        ),
                        eligible_row(),
                        performance_row(
                            id="00000000-0000-4000-8000-000000000005",
                            sales_status="S",
                            availability_num="9",
                        ),
                    ]
                ),
            ),
            response(200, seat_map_html()),
        ]
    )

    report = await run_smoke(FILM_URL, profile="chrome", session=session, clock=clock())

    assert report.performance_id == PERFORMANCE_ID
    assert session.calls[-1] == seat_map_url(PERFORMANCE_ID)
    assert sum(1 for url in session.calls if "mapSelect.asp" in url) == 1
    assert report.eligible_performances == 2


async def test_no_eligible_performance_is_an_input_failure() -> None:
    session = FakeSession(
        [
            response(
                200,
                article_html([performance_row(sales_status="C", availability_num="0")]),
            )
        ]
    )

    with pytest.raises(InputError):
        await run_smoke(FILM_URL, profile="chrome", session=session, clock=clock())

    assert all("mapSelect.asp" not in url for url in session.calls)


def test_no_eligible_performance_exits_2() -> None:
    session = FakeSession(
        [response(200, article_html([performance_row(sales_status="C", availability_num="0")]))]
    )

    assert main([FILM_URL], session=session, clock=clock()) == EXIT_INPUT


# ---------------------------------------------------------------------------
# Contract enforcement
# ---------------------------------------------------------------------------


async def test_requires_http_200() -> None:
    session = FakeSession(
        [
            response(204, article_html()),
            response(200, seat_map_html()),
        ]
    )

    with pytest.raises(BfiContractError, match="204"):
        await run_smoke(FILM_URL, profile="chrome", session=session, clock=clock())


async def test_requires_exact_agreement_between_reported_and_parsed_availability() -> None:
    session = FakeSession(
        [
            response(200, article_html([eligible_row(availability_num="3")])),
            response(200, seat_map_html()),
        ]
    )

    with pytest.raises(BfiContractError, match="availability"):
        await run_smoke(FILM_URL, profile="chrome", session=session, clock=clock())


async def test_requires_every_seat_to_carry_a_row_a_section_and_a_price_zone() -> None:
    incomplete = seat_map_html(
        extra_circles=(
            '<circle id="seat-3" data-status="S" data-seat-section="BFI IMAX" '
            'data-seat-row="" data-seat-seat="19" cx="368" cy="180"/>'
        )
    )
    session = FakeSession([response(200, article_html()), response(200, incomplete)])

    with pytest.raises(BfiContractError, match="row"):
        await run_smoke(FILM_URL, profile="chrome", session=session, clock=clock())


async def test_requires_every_seat_status_code_to_be_recognised() -> None:
    unknown_status = seat_map_html(
        extra_circles=(
            '<circle id="seat-4" data-status="Z" data-seat-section="BFI IMAX" '
            'data-seat-row="L" data-seat-seat="20" cx="382" cy="180"/>'
        )
    )
    session = FakeSession([response(200, article_html()), response(200, unknown_status)])

    with pytest.raises(BfiContractError, match="status"):
        await run_smoke(FILM_URL, profile="chrome", session=session, clock=clock())


# ---------------------------------------------------------------------------
# Final fix wave: the access-note signal is live-verified, not fixture-verified
# ---------------------------------------------------------------------------


async def test_requires_the_seat_map_to_carry_at_least_one_access_note() -> None:
    """No note anywhere means the attribute stopped being read, or stopped being sent.

    A BFI IMAX map always carries wheelchair spaces and their companion seats, and each
    of them carries a ``data-tsmessage``. A run that finds none of them has either lost
    the wire signal or is reading an attribute the site does not serve -- which is
    exactly the defect that a fixture pinning an invented attribute could not detect.
    """
    session = FakeSession(
        [
            response(200, article_html([eligible_row(availability_num="1")])),
            response(200, seat_map_html(access_note=None)),
        ]
    )

    with pytest.raises(BfiContractError, match="access note"):
        await run_smoke(FILM_URL, profile="chrome", session=session, clock=clock())


async def test_requires_every_access_note_seat_to_parse_as_restricted() -> None:
    """The note is what makes the seat unofferable; parsing it as available offers it."""
    session = FakeSession(
        [
            response(200, article_html([eligible_row(availability_num="3")])),
            response(
                200,
                seat_map_html(
                    extra_circles=(
                        '<circle id="seat-obstructed" data-status="A" '
                        'data-seat-section="BFI IMAX" data-seat-row="M" data-seat-seat="4" '
                        'data-tsmessage="This is a wheelchair space" cx="10" cy="20"/>'
                    )
                ),
            ),
        ]
    )

    report = await run_smoke(FILM_URL, profile="chrome", session=session, clock=clock())

    assert report.access_note_seats == 2
    assert report.restricted_seats == 2
    assert report.offerable_seats == 1


async def test_an_access_note_seat_left_offerable_is_a_contract_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the check has teeth: with the note ignored, the smoke must fail.

    ``is_restricted_access`` is the one place the note is turned into a status, so
    neutering it is the smallest faithful stand-in for BFI renaming the attribute or the
    parser reading the wrong one.
    """
    monkeypatch.setattr(
        "cinema_friend.bfi.seat_map.is_restricted_access",
        lambda zone_label, note: False,
    )
    session = FakeSession(
        [
            response(200, article_html()),
            response(200, seat_map_html()),
        ]
    )

    with pytest.raises(BfiContractError, match="RESTRICTED"):
        await run_smoke(FILM_URL, profile="chrome", session=session, clock=clock())


def test_contract_mismatch_exits_4() -> None:
    session = FakeSession(
        [
            response(200, article_html([eligible_row(availability_num="3")])),
            response(200, seat_map_html()),
        ]
    )

    assert main([FILM_URL], session=session, clock=clock()) == EXIT_CONTRACT


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


def test_challenge_exits_3() -> None:
    session = FakeSession([response(429, "slow down")])

    assert main([FILM_URL], session=session, clock=clock()) == EXIT_CHALLENGE


def test_interstitial_200_exits_3() -> None:
    session = FakeSession([response(200, "<html>Just a moment...</html>")])

    assert main([FILM_URL], session=session, clock=clock()) == EXIT_CHALLENGE


def test_network_failure_exits_5() -> None:
    session = FakeSession([FakeNetworkError("connection reset")] * 4)

    assert main([FILM_URL], session=session, clock=clock()) == EXIT_NETWORK


# ---------------------------------------------------------------------------
# Reporting and resource ownership
# ---------------------------------------------------------------------------


def test_passing_contract_exits_0_and_reports_profile_and_counts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = passing_session()

    assert main([FILM_URL, "--profile", "chrome"], session=session, clock=clock()) == EXIT_OK

    out = capsys.readouterr().out
    assert "profile: chrome" in out
    assert "film: dog-stars" in out
    assert "article pages fetched: 1" in out
    assert "performances parsed: 1" in out
    assert "seat maps fetched: 1" in out
    assert "seats parsed: 3" in out
    assert "available seats: 2" in out
    assert "offerable seats: 1" in out
    assert "reported availability: 2" in out
    assert f"performance: {PERFORMANCE_ID}" in out


def test_report_carries_no_token_and_no_document_body(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = FakeSession(
        [
            response(200, article_html([eligible_row()], current_page=1, total_pages=2)),
            response(200, article_html([eligible_row()], current_page=2, total_pages=2)),
            response(200, seat_map_html()),
        ]
    )

    assert main([FILM_URL], session=session, clock=clock()) == EXIT_OK

    captured = capsys.readouterr()
    printed = captured.out + captured.err
    assert TOKEN not in printed, "the transient sToken must never be printed"
    assert "articleContext" not in printed
    assert "<circle" not in printed
    assert "<svg" not in printed


async def test_report_counts_seats_and_availability() -> None:
    report = await run_smoke(
        FILM_URL, profile="chrome", session=passing_session(), clock=clock()
    )

    assert report.profile == "chrome"
    assert report.slug == SLUG
    assert report.seats_parsed == 3, "the duplicated outline/fill circle must collapse to one seat"
    assert report.available_seats == 2
    assert report.offerable_seats == 1, "the accessible space is available but not offerable"
    assert report.access_note_seats == 1
    assert report.reported_availability == 2
    assert report.price_zones == 1
    assert report.performance_id == PERFORMANCE_ID


def test_closes_the_session_on_success() -> None:
    session = passing_session()

    assert main([FILM_URL], session=session, clock=clock()) == EXIT_OK
    assert session.closed


def test_closes_the_session_when_the_contract_fails() -> None:
    session = FakeSession(
        [
            response(200, article_html([eligible_row(availability_num="3")])),
            response(200, seat_map_html()),
        ]
    )

    assert main([FILM_URL], session=session, clock=clock()) == EXIT_CONTRACT
    assert session.closed


def test_closes_the_session_when_the_url_is_rejected() -> None:
    session = FakeSession([])

    assert main(["https://example.com/nope"], session=session, clock=clock()) == EXIT_INPUT
    assert session.closed


# ---------------------------------------------------------------------------
# Token safety on the failure paths
# ---------------------------------------------------------------------------
#
# Success was already proven quiet. Failure is the harder case: every pagination URL
# carries the transient ``sToken``, and a failure message that interpolates the URL it
# was fetching puts that token on the terminal, into the launchd log, and into any
# journal that scrapes it. Page two is where that first becomes possible, so page two is
# where each failure class is provoked.


def paginated_session(*after_page_one: Any) -> FakeSession:
    """A two-page listing whose second page fails in the supplied way."""
    return FakeSession(
        [response(200, article_html([eligible_row()], current_page=1, total_pages=2)),
         *after_page_one]
    )


def assert_token_free(
    captured: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    output = captured.readouterr()
    printed = output.out + output.err
    logged = "\n".join(record.getMessage() for record in caplog.records) + caplog.text
    assert TOKEN not in printed, "the transient sToken must never be printed"
    assert TOKEN not in logged, "the transient sToken must never be logged"
    assert "sToken" not in printed
    assert printed.strip(), "a failure must still say something useful"


def test_a_page_two_challenge_reports_without_the_pagination_token(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    session = paginated_session(response(429, "slow down"))

    with caplog.at_level(logging.DEBUG):
        assert main([FILM_URL], session=session, clock=clock()) == EXIT_CHALLENGE

    assert len(session.calls) == 2, "the failure must happen on the pagination request"
    assert TOKEN in session.calls[1], "the token really is in the URL that failed"
    assert_token_free(capsys, caplog)


def test_a_page_two_network_failure_reports_without_the_pagination_token(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    session = paginated_session(*[FakeNetworkError("connection reset")] * 3)

    with caplog.at_level(logging.DEBUG):
        assert main([FILM_URL], session=session, clock=clock()) == EXIT_NETWORK

    assert TOKEN in session.calls[1]
    assert_token_free(capsys, caplog)


def test_a_client_failure_that_quotes_the_url_is_not_echoed(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """curl_cffi writes its own messages, and one of them may quote the whole URL.

    The transport's own messages are token-free by construction, but the smoke command
    prints exception text it did not write, so it redacts what it prints rather than
    trusting every library in the stack to have been careful.
    """
    leaky = FakeNetworkError(
        f"Failed to perform, curl: (56) Recv failure on "
        f"{pagination_url(TOKEN, 2, ARTICLE_ID)}"
    )
    session = paginated_session(*[leaky] * 3)

    with caplog.at_level(logging.DEBUG):
        assert main([FILM_URL], session=session, clock=clock()) == EXIT_NETWORK

    assert_token_free(capsys, caplog)


def test_a_contract_failure_reports_without_the_pagination_token(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    session = FakeSession(
        [
            response(200, article_html([eligible_row()], current_page=1, total_pages=2)),
            response(204, article_html([eligible_row()], current_page=2, total_pages=2)),
            response(200, seat_map_html()),
        ]
    )

    with caplog.at_level(logging.DEBUG):
        assert main([FILM_URL], session=session, clock=clock()) == EXIT_CONTRACT

    assert_token_free(capsys, caplog)


# ---------------------------------------------------------------------------
# Fix round 2 -- the smoke runs against the captured live page
# ---------------------------------------------------------------------------


async def test_runs_end_to_end_against_the_captured_live_article_page() -> None:
    """The smoke must clear the real BFI page, not just a hand-authored one.

    This is the regression that the invented schema would have failed: the fixture is a
    verbatim capture of a live ``articleContext``, so the parser can only satisfy it by
    reading BFI's own field names. The seat map is generated to carry exactly as many
    available seats as the captured row reports, which is the equality the smoke exists
    to check.
    """
    performance = performance_from_row(article_context_row_mappings()[0])
    session = FakeSession(
        [
            response(200, real_article_html()),
            response(200, real_article_html(current_page=2)),
            response(
                200,
                seat_map_html(
                    performance_id=performance.performance_id,
                    extra_circles=available_circles(performance.availability_num - 2),
                ),
            ),
        ]
    )

    report = await run_smoke(FILM_URL, profile="chrome", session=session, clock=clock())

    assert report.performance_id == performance.performance_id
    assert report.reported_availability == performance.availability_num
    assert report.available_seats == performance.availability_num
    assert report.article_pages == 2
    assert report.seat_maps == 1


async def test_an_unpublished_count_is_never_chosen_as_eligible() -> None:
    """``availability_num=-1`` means "not saying", so it offers nothing to check.

    The row is on sale and reserved-seating, so only the count keeps it out. If the
    sentinel were ever carried through as ``-1`` rather than normalised to zero, an
    ``!= 0`` style check would pick this row and the smoke would then compare -1 against
    a real seat count.
    """
    session = FakeSession(
        [
            response(
                200,
                article_html(
                    [
                        performance_row(
                            id=OTHER_PERFORMANCE_ID,
                            sales_status="S",
                            availability_status="U",
                            availability_num="-1",
                        )
                    ]
                ),
            )
        ]
    )

    with pytest.raises(InputError, match="no on-sale reserved performance"):
        await run_smoke(FILM_URL, profile="chrome", session=session, clock=clock())
