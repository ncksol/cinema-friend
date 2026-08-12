"""Tests for cinema_friend.logging_config: JSON records and redaction.

The point of these tests is that a log line is a *record*, not a sentence: every field
an operator needs (event, level, timestamp, watch/check identity, status, byte counts,
correlation id) is a separate key, and the things that must never be written down --
the bot token, a Cloudflare ``sToken``, a raw HTML or SVG body -- cannot reach the
stream by any route, whether they arrive as a structured field or interpolated into a
message.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest

from cinema_friend.logging_config import (
    REDACTED,
    JsonFormatter,
    RedactingFilter,
    configure_logging,
    correlation_scope,
)

TOKEN = "8012345678:AAF-secret-bot-token-value"


@pytest.fixture
def stream() -> io.StringIO:
    return io.StringIO()


@pytest.fixture
def logger(stream: io.StringIO) -> Iterator[logging.Logger]:
    """A logger wired exactly the way :func:`configure_logging` wires the root logger."""
    instance = logging.getLogger("cinema_friend.test")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter(secrets=(TOKEN,)))
    instance.addHandler(handler)
    instance.setLevel(logging.DEBUG)
    instance.propagate = False
    try:
        yield instance
    finally:
        instance.removeHandler(handler)


def records(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_record_is_json_with_event_level_and_timestamp(
    logger: logging.Logger, stream: io.StringIO
) -> None:
    logger.info("check completed")

    (record,) = records(stream)
    assert record["event"] == "check completed"
    assert record["level"] == "INFO"
    assert record["logger"] == "cinema_friend.test"
    assert str(record["timestamp"]).endswith("+00:00")


def test_structured_fields_are_separate_keys(
    logger: logging.Logger, stream: io.StringIO
) -> None:
    logger.info(
        "check completed",
        extra={
            "watch_id": "watch-1",
            "check_run_id": "run-1",
            "status": "active",
            "outcome": "success",
            "byte_count": 2048,
        },
    )

    (record,) = records(stream)
    assert record["watch_id"] == "watch-1"
    assert record["check_run_id"] == "run-1"
    assert record["status"] == "active"
    assert record["outcome"] == "success"
    assert record["byte_count"] == 2048


@pytest.mark.parametrize(
    "key",
    ["token", "telegram_bot_token", "secret", "client_secret", "sToken", "body", "html", "svg"],
)
def test_sensitive_keys_are_redacted(
    logger: logging.Logger, stream: io.StringIO, key: str
) -> None:
    logger.info("fetched document", extra={key: "s3cr3t-payload"})

    (record,) = records(stream)
    assert record[key] == REDACTED
    assert "s3cr3t-payload" not in stream.getvalue()


def test_secret_values_never_reach_the_stream_through_a_message(
    logger: logging.Logger, stream: io.StringIO
) -> None:
    logger.error("telegram rejected token %s", TOKEN)

    assert TOKEN not in stream.getvalue()
    (record,) = records(stream)
    assert REDACTED in str(record["event"])


def test_secret_values_never_reach_the_stream_through_a_traceback(
    logger: logging.Logger, stream: io.StringIO
) -> None:
    try:
        raise RuntimeError(f"invalid token {TOKEN}")
    except RuntimeError:
        logger.exception("startup failed")

    assert TOKEN not in stream.getvalue()


def test_correlation_id_is_attached_from_the_active_scope(
    logger: logging.Logger, stream: io.StringIO
) -> None:
    with correlation_scope("watch-42"):
        logger.info("check started")
    logger.info("loop idle")

    scoped, unscoped = records(stream)
    assert scoped["correlation_id"] == "watch-42"
    assert "correlation_id" not in unscoped


def test_correlation_scope_restores_the_previous_value(
    logger: logging.Logger, stream: io.StringIO
) -> None:
    with correlation_scope("outer"), correlation_scope("inner"):
        logger.info("inner event")
    with correlation_scope("outer"):
        logger.info("outer event")

    inner, outer = records(stream)
    assert inner["correlation_id"] == "inner"
    assert outer["correlation_id"] == "outer"


def test_configure_logging_installs_one_handler_and_is_repeatable(
    stream: io.StringIO,
) -> None:
    root = logging.getLogger()
    original = list(root.handlers)
    original_level = root.level
    try:
        configure_logging("DEBUG", secrets=(TOKEN,), stream=stream)
        configure_logging("WARNING", secrets=(TOKEN,), stream=stream)

        installed = [handler for handler in root.handlers if handler.name == "cinema-friend"]
        assert len(installed) == 1
        assert root.level == logging.WARNING

        logging.getLogger("cinema_friend.app").warning("token is %s", TOKEN)
        assert TOKEN not in stream.getvalue()
        assert records(stream)[0]["event"].__class__ is str
    finally:
        for handler in list(root.handlers):
            if handler not in original:
                root.removeHandler(handler)
        for handler in original:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(original_level)


def test_configure_logging_rejects_an_unknown_level(stream: io.StringIO) -> None:
    root = logging.getLogger()
    original = list(root.handlers)
    original_level = root.level
    try:
        with pytest.raises(ValueError):
            configure_logging("CHATTY", stream=stream)
    finally:
        for handler in list(root.handlers):
            if handler not in original:
                root.removeHandler(handler)
        root.setLevel(original_level)


# ---------------------------------------------------------------------------
# URL queries
# ---------------------------------------------------------------------------


PAGE_TOKEN = "LOGGED-STOKEN-DO-NOT-WRITE"
PAGE_URL = (
    "https://whatson.bfi.org.uk/imax/Online/default.asp"
    f"?sToken={PAGE_TOKEN}&BOset::WScontent::SearchResultsInfo::current_page=2"
)


def test_a_url_in_a_message_keeps_its_path_and_loses_its_query(
    logger: logging.Logger, stream: io.StringIO
) -> None:
    """A BFI URL is only loggable up to its path; the query holds the transient token."""
    logger.warning("could not fetch %s", PAGE_URL)

    event = str(records(stream)[0]["event"])
    assert PAGE_TOKEN not in event
    assert "sToken" not in event
    assert "whatson.bfi.org.uk/imax/Online/default.asp" in event


def test_a_url_inside_a_traceback_loses_its_query(
    logger: logging.Logger, stream: io.StringIO
) -> None:
    try:
        raise RuntimeError(f"Recv failure on {PAGE_URL}")
    except RuntimeError:
        logger.exception("fetch failed")

    error = str(records(stream)[0]["error"])
    assert PAGE_TOKEN not in error
    assert "whatson.bfi.org.uk/imax/Online/default.asp" in error


def test_a_fragment_is_dropped_with_the_query(logger: logging.Logger, stream: io.StringIO) -> None:
    logger.info("redirected to https://whatson.bfi.org.uk/imax/Online/x.asp#SECRET-FRAGMENT rest")

    event = str(records(stream)[0]["event"])
    assert "SECRET-FRAGMENT" not in event
    assert "rest" in event, "only the URL's own query and fragment are removed"
