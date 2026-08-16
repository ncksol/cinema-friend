"""Tests for scripts/install_launch_agent.py: what gets written and what gets run.

A LaunchAgent is a file plus two ``launchctl`` invocations, and every way this can go
wrong is a way the service silently never starts. So the properties under test are:

- The plist is addressed to one fixed label and points at absolute paths only. ``launchd``
  has no shell, no ``PATH``, and no current directory of its own: a relative path in a
  plist is not a smaller mistake than a wrong one.
- The executable is the one belonging to the interpreter running the installer, found as
  a sibling of ``sys.executable``. Nothing is hard-coded to a checkout.
- The env file is validated as user-owned mode ``0600`` *before* the plist is written,
  because the plist is what makes a world-readable token file get loaded every login.
- ``launchctl`` is invoked with argument arrays. No string is ever handed to a shell, so
  a path containing a space or a quote is data, not syntax.
- Nothing that belongs to a browser, a debugger, or a proxy appears anywhere in the
  rendered file.
- Uninstall removes both halves and is safe to run twice.
"""

from __future__ import annotations

import importlib.util
import os
import plistlib
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_launch_agent.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("install_launch_agent", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent = _load()

FORBIDDEN = (
    "--remote-debugging-port",
    "remote-debugging",
    "9222",
    "chrome",
    "chromium",
    "browser",
    "headless",
    "proxy",
    "PROXY",
)


class RecordingLaunchctl:
    """Captures the argument arrays an install/uninstall would hand to ``launchctl``."""

    def __init__(self, missing: bool = False) -> None:
        self.calls: list[tuple[Sequence[str], bool]] = []
        self.missing = missing

    def __call__(self, argv: Sequence[str], *, allow_missing: bool = False) -> None:
        self.calls.append((list(argv), allow_missing))
        if self.missing and argv[0] == "bootout" and not allow_missing:
            raise AssertionError("a missing service must be booted out tolerantly")

    @property
    def commands(self) -> list[list[str]]:
        return [list(argv) for argv, _ in self.calls]


def _write_valid_env(path: Path, *, token: str = "123:abc") -> None:
    path.write_text(
        "\n".join([
            f"TELEGRAM_BOT_TOKEN={token}",
            "TELEGRAM_ALLOWED_USER_IDS=11,12",
            f"DATABASE_PATH={path.parent / 'cinema-friend.db'}",
            "LOG_LEVEL=INFO",
            "BFI_IMPERSONATE_PROFILE=chrome",
        ])
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / "cinema-friend.env"
    _write_valid_env(path)
    return path


@pytest.fixture
def venv_bin(tmp_path: Path) -> Path:
    """A directory shaped like a virtualenv ``bin``: an interpreter and its console script."""
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    executable = bin_dir / "cinema-friend"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    return bin_dir


def rendered(tmp_path: Path, env_file: Path, venv_bin: Path) -> tuple[str, dict[str, Any]]:
    text = agent.render_plist(
        executable=venv_bin / "cinema-friend",
        env_file=env_file,
        working_directory=tmp_path,
        stdout_path=tmp_path / "logs" / "cinema-friend.out.log",
        stderr_path=tmp_path / "logs" / "cinema-friend.err.log",
    )
    return text, plistlib.loads(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_label_is_the_fixed_service_identifier(
    tmp_path: Path, env_file: Path, venv_bin: Path
) -> None:
    _, plist = rendered(tmp_path, env_file, venv_bin)

    assert agent.LABEL == "com.ncksol.cinema-friend"
    assert plist["Label"] == "com.ncksol.cinema-friend"


def test_program_arguments_are_the_venv_executable_and_absolute_env_file(
    tmp_path: Path, env_file: Path, venv_bin: Path
) -> None:
    _, plist = rendered(tmp_path, env_file, venv_bin)

    assert plist["ProgramArguments"] == [
        str(venv_bin / "cinema-friend"),
        "--env-file",
        str(env_file),
    ]
    assert Path(plist["ProgramArguments"][0]).is_absolute()
    assert Path(plist["ProgramArguments"][2]).is_absolute()


def test_run_at_load_and_keep_alive_are_true(
    tmp_path: Path, env_file: Path, venv_bin: Path
) -> None:
    _, plist = rendered(tmp_path, env_file, venv_bin)

    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True


def test_working_directory_and_log_paths_are_absolute(
    tmp_path: Path, env_file: Path, venv_bin: Path
) -> None:
    _, plist = rendered(tmp_path, env_file, venv_bin)

    for key in ("WorkingDirectory", "StandardOutPath", "StandardErrorPath"):
        assert Path(plist[key]).is_absolute(), f"{key} must be absolute"


def test_no_browser_debug_or_proxy_arguments_are_rendered() -> None:
    # Fixed synthetic paths: rendering touches no filesystem, and a temporary directory
    # named after this test would contain the very words being searched for.
    text = agent.render_plist(
        executable=Path("/opt/cf/bin/cinema-friend"),
        env_file=Path("/opt/cf/cinema-friend.env"),
        working_directory=Path("/opt/cf"),
        stdout_path=Path("/opt/cf/logs/out.log"),
        stderr_path=Path("/opt/cf/logs/err.log"),
    )

    lowered = text.lower()
    for forbidden in FORBIDDEN:
        assert forbidden.lower() not in lowered, f"{forbidden!r} must never appear in the plist"


def test_rendered_plist_escapes_paths_rather_than_quoting_them(
    tmp_path: Path, env_file: Path, venv_bin: Path
) -> None:
    awkward = tmp_path / "a dir & <one>"
    awkward.mkdir()

    text = agent.render_plist(
        executable=venv_bin / "cinema-friend",
        env_file=env_file,
        working_directory=awkward,
        stdout_path=awkward / "out.log",
        stderr_path=awkward / "err.log",
    )
    plist = plistlib.loads(text.encode("utf-8"))

    assert plist["WorkingDirectory"] == str(awkward)
    assert "&amp;" in text and "&lt;one&gt;" in text


# ---------------------------------------------------------------------------
# Resolution and validation
# ---------------------------------------------------------------------------


def test_executable_is_resolved_beside_the_running_interpreter(venv_bin: Path) -> None:
    resolved = agent.resolve_executable(venv_bin / "python")

    assert resolved == (venv_bin / "cinema-friend").resolve()
    assert resolved.is_absolute()


def test_resolution_fails_when_the_console_script_is_absent(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bare" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")

    with pytest.raises(agent.InstallError, match="cinema-friend"):
        agent.resolve_executable(bin_dir / "python")


def test_resolution_defaults_to_the_current_interpreter() -> None:
    expected = (Path(sys.executable).parent / "cinema-friend").resolve()

    assert agent.resolve_executable() == expected


def test_env_file_must_be_mode_0600(tmp_path: Path, env_file: Path) -> None:
    env_file.chmod(0o644)

    with pytest.raises(agent.InstallError, match="0600"):
        agent.validate_env_file(env_file)


def test_env_file_must_exist(tmp_path: Path) -> None:
    with pytest.raises(agent.InstallError):
        agent.validate_env_file(tmp_path / "absent.env")


def test_env_file_is_returned_absolute(
    tmp_path: Path, env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    resolved = agent.validate_env_file(Path(env_file.name))

    assert resolved == env_file.resolve()
    assert resolved.is_absolute()


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def test_install_writes_the_plist_and_bootstraps_with_argument_arrays(
    tmp_path: Path, env_file: Path, venv_bin: Path
) -> None:
    agents_dir = tmp_path / "LaunchAgents"
    log_dir = tmp_path / "Logs"
    launchctl = RecordingLaunchctl()

    plist_path = agent.install(
        env_file=env_file,
        python_executable=venv_bin / "python",
        working_directory=tmp_path,
        agents_dir=agents_dir,
        log_dir=log_dir,
        launchctl=launchctl,
    )

    assert plist_path == agents_dir / "com.ncksol.cinema-friend.plist"
    assert plist_path.is_file()
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["ProgramArguments"][0] == str((venv_bin / "cinema-friend").resolve())
    assert log_dir.is_dir()

    uid = os.getuid()
    assert launchctl.commands == [
        ["bootout", f"gui/{uid}/com.ncksol.cinema-friend"],
        ["bootstrap", f"gui/{uid}", str(plist_path)],
    ]
    for command, _ in launchctl.calls:
        assert isinstance(command, list), "launchctl must be invoked with an argument array"
        assert all(isinstance(arg, str) for arg in command)


def test_install_tolerates_a_service_that_was_not_already_loaded(
    tmp_path: Path, env_file: Path, venv_bin: Path
) -> None:
    launchctl = RecordingLaunchctl(missing=True)

    agent.install(
        env_file=env_file,
        python_executable=venv_bin / "python",
        working_directory=tmp_path,
        agents_dir=tmp_path / "LaunchAgents",
        log_dir=tmp_path / "Logs",
        launchctl=launchctl,
    )

    assert launchctl.calls[0][1] is True, "the pre-install bootout must tolerate a missing service"


def test_install_refuses_a_world_readable_env_file_before_writing_anything(
    tmp_path: Path, env_file: Path, venv_bin: Path
) -> None:
    env_file.chmod(0o644)
    agents_dir = tmp_path / "LaunchAgents"
    launchctl = RecordingLaunchctl()

    with pytest.raises(agent.InstallError, match="0600"):
        agent.install(
            env_file=env_file,
            python_executable=venv_bin / "python",
            working_directory=tmp_path,
            agents_dir=agents_dir,
            log_dir=tmp_path / "Logs",
            launchctl=launchctl,
        )

    assert not agents_dir.exists()
    assert launchctl.calls == []


def test_installed_plist_is_not_group_or_world_writable(
    tmp_path: Path, env_file: Path, venv_bin: Path
) -> None:
    plist_path = agent.install(
        env_file=env_file,
        python_executable=venv_bin / "python",
        working_directory=tmp_path,
        agents_dir=tmp_path / "LaunchAgents",
        log_dir=tmp_path / "Logs",
        launchctl=RecordingLaunchctl(),
    )

    assert not plist_path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def test_install_does_not_copy_the_env_file_contents_into_the_plist(
    tmp_path: Path, env_file: Path, venv_bin: Path
) -> None:
    _write_valid_env(env_file, token="8012345678:AAF-secret-value")

    plist_path = agent.install(
        env_file=env_file,
        python_executable=venv_bin / "python",
        working_directory=tmp_path,
        agents_dir=tmp_path / "LaunchAgents",
        log_dir=tmp_path / "Logs",
        launchctl=RecordingLaunchctl(),
    )

    assert "AAF-secret-value" not in plist_path.read_text(encoding="utf-8")


def test_install_rejects_invalid_settings_before_writing_or_loading(
    tmp_path: Path, venv_bin: Path
) -> None:
    env_file = tmp_path / "incomplete.env"
    env_file.write_text(
        "\n".join([
            "TELEGRAM_ALLOWED_USER_IDS=11",
            f"DATABASE_PATH={tmp_path / 'cinema-friend.db'}",
        ])
        + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    agents_dir = tmp_path / "LaunchAgents"
    launchctl = RecordingLaunchctl()

    with pytest.raises(agent.InstallError, match="TELEGRAM_BOT_TOKEN"):
        agent.install(
            env_file=env_file,
            python_executable=venv_bin / "python",
            agents_dir=agents_dir,
            log_dir=tmp_path / "Logs",
            launchctl=launchctl,
        )

    assert not agents_dir.exists()
    assert launchctl.calls == []


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


def test_uninstall_boots_out_and_removes_the_plist(
    tmp_path: Path, env_file: Path, venv_bin: Path
) -> None:
    agents_dir = tmp_path / "LaunchAgents"
    plist_path = agent.install(
        env_file=env_file,
        python_executable=venv_bin / "python",
        working_directory=tmp_path,
        agents_dir=agents_dir,
        log_dir=tmp_path / "Logs",
        launchctl=RecordingLaunchctl(),
    )
    launchctl = RecordingLaunchctl()

    agent.uninstall(agents_dir=agents_dir, launchctl=launchctl)

    uid = os.getuid()
    assert launchctl.commands == [["bootout", f"gui/{uid}/com.ncksol.cinema-friend"]]
    assert not plist_path.exists()


def test_uninstall_is_idempotent(tmp_path: Path) -> None:
    agents_dir = tmp_path / "LaunchAgents"
    launchctl = RecordingLaunchctl(missing=True)

    agent.uninstall(agents_dir=agents_dir, launchctl=launchctl)
    agent.uninstall(agents_dir=agents_dir, launchctl=launchctl)

    assert all(allow_missing for _, allow_missing in launchctl.calls)


# ---------------------------------------------------------------------------
# Lazy application imports
# ---------------------------------------------------------------------------


def test_module_import_does_not_pull_in_cinema_friend() -> None:
    """``uninstall`` must be able to recover from a damaged runtime package, so importing
    this module at all must not import ``cinema_friend``."""
    assert not hasattr(agent, "load_settings")
    assert not hasattr(agent, "InputError")


def _block_cinema_friend(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def _blocking_import(name: str, *args: Any, **kwargs: Any) -> ModuleType:
        if name == "cinema_friend" or name.startswith("cinema_friend."):
            raise ImportError(f"blocked for test: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)


def test_module_loads_when_cinema_friend_is_unimportable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_cinema_friend(monkeypatch)

    module = _load()  # must not raise despite cinema_friend being blocked

    assert module.LABEL == agent.LABEL


def test_uninstall_works_when_cinema_friend_is_unimportable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _block_cinema_friend(monkeypatch)
    module = _load()
    launchctl = RecordingLaunchctl(missing=True)

    module.uninstall(agents_dir=tmp_path / "LaunchAgents", launchctl=launchctl)

    assert all(allow_missing for _, allow_missing in launchctl.calls)


def test_install_still_requires_cinema_friend_for_validation(
    tmp_path: Path, env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``load_settings`` remains the install-time authority: blocking ``cinema_friend``
    must not silently skip validation, it must fail loudly the moment install needs it."""
    _block_cinema_friend(monkeypatch)
    module = _load()

    with pytest.raises(ImportError, match="cinema_friend"):
        module.validate_env_file(env_file)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_reports_a_bad_env_file_without_a_traceback(
    tmp_path: Path, env_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file.chmod(0o644)

    code = agent.main(
        ["install", "--env-file", str(env_file)],
        launchctl=RecordingLaunchctl(),
    )

    assert code == 2
    assert "0600" in capsys.readouterr().err
