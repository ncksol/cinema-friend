"""Command line entry point: load a private env file, configure logging, and run.

Kept deliberately thin. Everything it does beyond argument parsing lives in
:mod:`cinema_friend.app`, so the lifecycle can be tested without a process.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from cinema_friend.app import load_settings, run_app
from cinema_friend.config import Settings
from cinema_friend.domain.errors import InputError
from cinema_friend.logging_config import configure_logging

logger = logging.getLogger("cinema_friend.cli")


def _run(settings: Settings) -> None:
    asyncio.run(run_app(settings))


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[[Settings], None] = _run,
) -> int:
    parser = argparse.ArgumentParser(
        prog="cinema-friend",
        description="Watch BFI IMAX listings and message a Telegram user when seats appear.",
    )
    parser.add_argument(
        "--env-file",
        required=True,
        type=Path,
        help="path to a user-owned, mode 0600 file holding the bot token and settings",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.env_file)
    except InputError as error:
        # Printed rather than logged: logging is not configured yet, and the message is
        # about the file, never about what is inside it.
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    try:
        # The token is registered as a secret before anything else can log, so a library
        # that echoes a request URL or an auth failure cannot write it out.
        configure_logging(settings.log_level, secrets=(settings.telegram_bot_token,))
    except ValueError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    try:
        runner(settings)
    except Exception:
        # Reported through the redacting handler rather than left to the default
        # excepthook: an authentication failure from Telegram or curl carries the token
        # in its own message, and a raw traceback would print it in the clear.
        logger.exception("application exited with an error")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a process, not a test
    raise SystemExit(main())
