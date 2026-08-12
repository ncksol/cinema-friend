#!/usr/bin/env python3
"""Install or remove the ``launchd`` user agent that keeps cinema-friend running.

A LaunchAgent is a property list plus two ``launchctl`` calls, and almost every way it
fails is silent: ``launchd`` has no shell, no ``PATH``, and no working directory of its
own, so a relative path in the plist is not a smaller mistake than a wrong one. Three
rules follow, and this script exists to enforce them rather than trust a hand-written
file:

1. **Nothing is hard-coded to a checkout.** The executable is found as the sibling of
   the interpreter running this script, so the agent points at whichever virtual
   environment installed it, wherever that lives.
2. **Every path in the plist is absolute and resolved.** Rendering goes through
   :mod:`plistlib`, so a path containing a space, an ampersand, or a quote is XML-escaped
   data and never something a reader has to quote correctly.
3. **The env file is checked before the plist is written.** The plist is what causes that
   file to be read at every login; a token file the rest of the machine can read must be
   refused while it is still only one user's mistake.

``launchctl`` is always invoked with an argument array and an absolute binary path. No
string is ever handed to a shell.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Protocol

LABEL: Final = "com.ncksol.cinema-friend"
EXECUTABLE_NAME: Final = "cinema-friend"
LAUNCHCTL: Final = "/bin/launchctl"

_NO_SUCH_PROCESS: Final = 3
"""``launchctl``'s exit status when the service it was asked about is not loaded.

Tolerated only where "not loaded" is the desired end state: the pre-install cleanup and
uninstall. Every other status, on every call, is a failure.
"""

_PLIST_MODE: Final = 0o644
_LOG_DIR_MODE: Final = 0o700


class InstallError(Exception):
    """Raised when the environment cannot support a correct installation."""


class LaunchctlRunner(Protocol):
    """How this script talks to ``launchctl``, so an install can be tested without one."""

    def __call__(self, argv: Sequence[str], *, allow_missing: bool = False) -> None: ...


def run_launchctl(argv: Sequence[str], *, allow_missing: bool = False) -> None:
    """Invoke ``launchctl`` with an argument array.

    ``allow_missing`` accepts the single status that means "there was nothing to do",
    which is what makes install re-runnable and uninstall idempotent. Everything else
    raises.
    """
    command = [LAUNCHCTL, *argv]
    if not allow_missing:
        subprocess.run(command, check=True, capture_output=True, text=True)
        return
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode not in (0, _NO_SUCH_PROCESS):
        raise subprocess.CalledProcessError(
            completed.returncode, command, completed.stdout, completed.stderr
        )


# ---------------------------------------------------------------------------
# Resolution and validation
# ---------------------------------------------------------------------------


def resolve_executable(python_executable: Path | None = None) -> Path:
    """Return the absolute ``cinema-friend`` console script beside *python_executable*.

    Defaults to the interpreter running this script, which is the whole point: run it
    with a virtual environment's ``python`` and the agent is wired to that environment.

    The interpreter path is deliberately *not* resolved through symlinks first: a virtual
    environment's ``bin/python`` is a symlink to the base interpreter, so following it
    would look for the console script beside the system Python and never find the one
    that was just installed.
    """
    interpreter = Path(python_executable or sys.executable)
    if not interpreter.is_absolute():
        interpreter = interpreter.absolute()
    executable = interpreter.parent / EXECUTABLE_NAME
    if not executable.is_file():
        raise InstallError(
            f"no {EXECUTABLE_NAME} executable beside {interpreter}; "
            "install the project into this environment first (pip install -e .)"
        )
    return executable.resolve()


def validate_env_file(env_file: Path) -> Path:
    """Return *env_file* absolute, after proving it is a private, user-owned regular file.

    The same checks the service itself makes at startup, made here so a misconfigured
    deployment is refused at install time rather than at the next login.
    """
    path = Path(env_file).expanduser().resolve()
    try:
        info = os.stat(path)
    except OSError as error:
        raise InstallError(f"env file does not exist or cannot be read: {path}") from error
    if not stat.S_ISREG(info.st_mode):
        raise InstallError(f"env file is not a regular file: {path}")
    if info.st_uid != os.getuid():
        raise InstallError(f"env file must be owned by the installing user: {path}")
    if info.st_mode & 0o077:
        raise InstallError(
            f"env file must not be readable by group or others (mode 0600): {path}"
        )
    return path


def default_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def default_log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "cinema-friend"


def default_working_directory() -> Path:
    """The user's home directory.

    Nothing the service does depends on its working directory -- the env file and the
    database are absolute -- and home is the one directory guaranteed to exist for the
    life of the agent. Pointing it at a checkout would make ``KeepAlive`` restarts fail
    the day that checkout is moved.
    """
    return Path.home()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_plist(
    *,
    executable: Path,
    env_file: Path,
    working_directory: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> str:
    """Render the LaunchAgent property list as XML."""
    plist: dict[str, object] = {
        "Label": LABEL,
        "ProgramArguments": [str(executable), "--env-file", str(env_file)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(working_directory),
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
        # launchd starts the process with a minimal environment. Unbuffered stdio is what
        # makes the log files above readable while the service is still running.
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }
    return plistlib.dumps(plist, fmt=plistlib.FMT_XML).decode("utf-8")


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------


def install(
    *,
    env_file: Path,
    python_executable: Path | None = None,
    working_directory: Path | None = None,
    agents_dir: Path | None = None,
    log_dir: Path | None = None,
    launchctl: LaunchctlRunner = run_launchctl,
) -> Path:
    """Write the LaunchAgent and load it, replacing any previously loaded copy.

    Returns the path of the plist that was written. Validation happens first, so a
    refused install leaves nothing behind.
    """
    resolved_env = validate_env_file(env_file)
    executable = resolve_executable(python_executable)
    agents = (agents_dir or default_agents_dir()).expanduser().resolve()
    logs = (log_dir or default_log_dir()).expanduser().resolve()
    working = (working_directory or default_working_directory()).expanduser().resolve()

    logs.mkdir(parents=True, exist_ok=True)
    logs.chmod(_LOG_DIR_MODE)
    agents.mkdir(parents=True, exist_ok=True)

    plist_path = agents / f"{LABEL}.plist"
    plist_path.write_text(
        render_plist(
            executable=executable,
            env_file=resolved_env,
            working_directory=working,
            stdout_path=logs / "cinema-friend.out.log",
            stderr_path=logs / "cinema-friend.err.log",
        ),
        encoding="utf-8",
    )
    plist_path.chmod(_PLIST_MODE)

    # Bootstrapping a label that is already loaded fails, so an install over a running
    # service unloads it first. It is not an error for there to be nothing loaded.
    launchctl(["bootout", f"gui/{os.getuid()}/{LABEL}"], allow_missing=True)
    launchctl(["bootstrap", f"gui/{os.getuid()}", str(plist_path)])
    return plist_path


def uninstall(
    *,
    agents_dir: Path | None = None,
    launchctl: LaunchctlRunner = run_launchctl,
) -> Path:
    """Unload the LaunchAgent and remove its plist. Safe to run when neither exists."""
    agents = (agents_dir or default_agents_dir()).expanduser().resolve()
    plist_path = agents / f"{LABEL}.plist"
    launchctl(["bootout", f"gui/{os.getuid()}/{LABEL}"], allow_missing=True)
    plist_path.unlink(missing_ok=True)
    return plist_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install_launch_agent.py",
        description=f"Install or remove the {LABEL} launchd user agent.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    installer = subparsers.add_parser("install", help="write and load the LaunchAgent")
    installer.add_argument(
        "--env-file",
        required=True,
        type=Path,
        help="path to the user-owned, mode 0600 file holding the bot token and settings",
    )
    installer.add_argument(
        "--working-directory",
        type=Path,
        default=None,
        help="working directory for the service (default: the current user's home)",
    )
    installer.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="directory for stdout/stderr logs (default: ~/Library/Logs/cinema-friend)",
    )

    subparsers.add_parser("uninstall", help="unload the LaunchAgent and remove its plist")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    launchctl: LaunchctlRunner = run_launchctl,
) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "install":
            plist_path = install(
                env_file=args.env_file,
                working_directory=args.working_directory,
                log_dir=args.log_dir,
                launchctl=launchctl,
            )
            print(f"installed and loaded {LABEL}: {plist_path}")
        else:
            plist_path = uninstall(launchctl=launchctl)
            print(f"unloaded and removed {LABEL}: {plist_path}")
    except InstallError as error:
        print(f"install error: {error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip() or f"exit status {error.returncode}"
        print(f"launchctl failed: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a process, not a test
    raise SystemExit(main())
