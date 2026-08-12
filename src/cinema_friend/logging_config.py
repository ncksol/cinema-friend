"""Structured JSON logging with redaction of anything that must not be written down.

A long-running watcher is only debuggable through its logs, and those logs are only
safe to keep if the bot token, a Cloudflare ``sToken``, and the raw pages fetched from
BFI never appear in them. Both goals are met at the same place: one handler that
formats records as JSON objects, and one filter that scrubs them first.

The filter deliberately does three different jobs, because a secret can arrive by three
different routes. A *structured* value is caught by its key -- anything whose name looks
like a token, a secret, or a raw body/html/svg payload becomes ``[redacted]``. A
*literal* value is caught by matching the registered secret strings anywhere in the
rendered message or its traceback, which covers the case where a token was interpolated
into a sentence or embedded in an exception raised by a library. A *transient* value --
BFI's per-session ``sToken``, which is not known in advance and so cannot be registered
-- is caught positionally: every URL in a rendered message or traceback is cut back to
its path, because everything after the ``?`` is request state that no log needs.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Final, TextIO

REDACTED: Final = "[redacted]"
HANDLER_NAME: Final = "cinema-friend"

SENSITIVE_KEY_RE: Final = re.compile(r"token|secret|password|cookie|body|html|svg", re.IGNORECASE)
"""Substring match, case-insensitive, so ``sToken`` and ``telegram_bot_token`` both hit.

Matching on a substring costs the occasional over-redaction -- a field called
``body_bytes`` would be hidden -- which is the right way round for a value that is
unrecoverable once logged.
"""

URL_QUERY_RE: Final = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s\"'<>]*?)[?#][^\s\"'<>]*")
"""One absolute URL, captured up to the first ``?`` or ``#`` that follows its scheme."""


def strip_url_queries(text: str) -> str:
    """Cut every URL in *text* back to scheme, host and path.

    A BFI pagination URL carries the transient ``sToken`` in its query string, so any
    sentence quoting a whole URL is a sentence carrying a token. This service writes
    its own messages with :func:`~cinema_friend.bfi.urls.describe_url` and never
    interpolates a raw URL, but it is not the only thing that writes: ``curl_cffi``,
    ``httpx`` and ``python-telegram-bot`` all put the URL they were given into their
    own exception messages, and those messages reach the log through an ``exc_info``
    nobody here wrote. Removing the query where the text is rendered covers all of it,
    including the libraries that have not been written yet.

    Only the URL's own query and fragment are removed; the surrounding sentence is left
    alone, so the line stays readable and still names the route that failed.
    """
    return URL_QUERY_RE.sub(lambda match: match.group(1), text)


_RESERVED: Final = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_correlation_id: ContextVar[str | None] = ContextVar("cinema_friend_correlation_id", default=None)


@contextmanager
def correlation_scope(value: str) -> Iterator[str]:
    """Tag every record emitted inside the block with *value*.

    Used to stamp a watch or check identity onto the whole of one unit of work, so the
    lines a single check produced can be pulled back out of an interleaved log.
    """
    token = _correlation_id.set(value)
    try:
        yield value
    finally:
        _correlation_id.reset(token)


def current_correlation_id() -> str | None:
    return _correlation_id.get()


class RedactingFilter(logging.Filter):
    """Removes sensitive structured fields and secret literals from a record.

    Rendering the message here rather than in the formatter is deliberate: substituting
    into ``msg`` only works once ``args`` have been applied, and a downstream handler
    must never get a chance to render the original.
    """

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)
        self._tracebacks = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in list(record.__dict__.items()):
            if key not in _RESERVED and SENSITIVE_KEY_RE.search(key):
                record.__dict__[key] = REDACTED

        record.msg = self._scrub(record.getMessage())
        record.args = ()
        if record.exc_info is not None:
            # Rendered here and the original discarded: a secret is just as likely to be
            # inside an exception message as inside the log call, and only the filter is
            # guaranteed to run before anything writes.
            record.exc_text = self._tracebacks.formatException(record.exc_info)
            record.exc_info = None
        if record.exc_text:
            record.exc_text = self._scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = self._scrub(record.stack_info)
        return True

    def scrub(self, text: str) -> str:
        return self._scrub(text)

    def _scrub(self, text: str) -> str:
        text = strip_url_queries(text)
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text


class JsonFormatter(logging.Formatter):
    """Writes one JSON object per record, with structured extras as top-level keys."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        correlation_id = getattr(record, "correlation_id", None) or current_correlation_id()
        if correlation_id is not None:
            payload["correlation_id"] = correlation_id
        for key, value in record.__dict__.items():
            if key in _RESERVED or key in payload or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["error"] = record.exc_text
        if record.stack_info:
            payload["stack"] = record.stack_info
        return json.dumps(payload, default=str)


def configure_logging(
    level: str,
    *,
    secrets: Sequence[str] = (),
    stream: TextIO | None = None,
) -> logging.Handler:
    """Install (or replace) the single JSON handler on the root logger.

    Replacing rather than appending keeps this safe to call twice -- a restarted app in
    the same process would otherwise double every line.
    """
    resolved = logging.getLevelNamesMapping().get(level.upper())
    if resolved is None:
        raise ValueError(f"unknown log level: {level}")

    root = logging.getLogger()
    for existing in [handler for handler in root.handlers if handler.name == HANDLER_NAME]:
        root.removeHandler(existing)
        existing.close()

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.name = HANDLER_NAME
    handler.setFormatter(JsonFormatter())
    redactor = RedactingFilter(secrets=secrets)
    handler.addFilter(redactor)
    root.addHandler(handler)
    root.setLevel(resolved)

    # These are chatty at DEBUG and their records carry request bodies we have no reason
    # to keep; the filter would redact them anyway, so silence the noise at the source.
    for noisy in ("httpx", "telegram.ext.Updater", "telegram.request"):
        logging.getLogger(noisy).setLevel(max(resolved, logging.INFO))

    return handler
