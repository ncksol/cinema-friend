"""Subprocess tests for the one-command macOS installer."""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "install.sh"
LABEL = "com.ncksol.cinema-friend"


@dataclass(slots=True)
class InstallerHarness:
    root: Path
    env: dict[str, str]
    calls: Path
    venv: Path
    env_file: Path


def _write_executable(path: Path, body: str) -> None:
    path.write_text(
        "#!/bin/bash\nset -eu\n" + textwrap.dedent(body).lstrip(),
        encoding="utf-8",
    )
    path.chmod(0o755)


@pytest.fixture
def installer(tmp_path: Path) -> InstallerHarness:
    bin_dir = tmp_path / "bin"
    home = tmp_path / "home"
    calls = tmp_path / "calls.log"
    venv = tmp_path / "venv"
    env_file = home / ".config" / "cinema-friend" / "cinema-friend.env"
    bin_dir.mkdir()
    home.mkdir()

    _write_executable(
        bin_dir / "python3.12",
        r"""
        printf 'python:%s\n' "$*" >> "$CINEMA_FRIEND_TEST_CALLS"
        if [[ "${1:-}" == "-c" ]]; then
            exit "${CINEMA_FRIEND_TEST_PYTHON_STATUS:-0}"
        fi
        if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
            destination="$3"
            mkdir -p "$destination/bin"
            cp "$0" "$destination/bin/python"
            chmod 755 "$destination/bin/python"
            printf '#!/bin/bash\nexit 0\n' > "$destination/bin/cinema-friend"
            chmod 755 "$destination/bin/cinema-friend"
        fi
        """,
    )
    (bin_dir / "python3").symlink_to("python3.12")
    _write_executable(
        bin_dir / "uname",
        r"""
        printf '%s\n' "${CINEMA_FRIEND_TEST_UNAME:-Darwin}"
        """,
    )
    _write_executable(
        bin_dir / "launchctl",
        r"""
        printf 'launchctl:%s\n' "$*" >> "$CINEMA_FRIEND_TEST_CALLS"
        if [[ -n "${CINEMA_FRIEND_TEST_LAUNCHCTL_OUTPUT:-}" ]]; then
            printf '%s\n' "$CINEMA_FRIEND_TEST_LAUNCHCTL_OUTPUT"
            exit 0
        fi
        if [[ -n "${CINEMA_FRIEND_TEST_LAUNCHCTL_RUNNING_ONCE:-}" ]]; then
            marker="$CINEMA_FRIEND_TEST_LAUNCHCTL_RUNNING_ONCE"
            if [[ ! -e "$marker" ]]; then
                : > "$marker"
                printf 'state = running\npid = 4242\n'
                exit 0
            fi
            exit 3
        fi
        if [[ "${CINEMA_FRIEND_TEST_LAUNCHCTL_RUNNING:-1}" == "1" ]]; then
            printf 'state = running\npid = 4242\n'
            exit 0
        fi
        exit 3
        """,
    )
    _write_executable(
        bin_dir / "id",
        """
        printf '501\n'
        """,
    )
    _write_executable(
        bin_dir / "sleep",
        r"""
        printf 'sleep:%s\n' "$*" >> "$CINEMA_FRIEND_TEST_CALLS"
        """,
    )

    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": f"{bin_dir}:{env.get('PATH', '')}",
        "CINEMA_FRIEND_UNAME": str(bin_dir / "uname"),
        "CINEMA_FRIEND_LAUNCHCTL": str(bin_dir / "launchctl"),
        "CINEMA_FRIEND_ID": str(bin_dir / "id"),
        "CINEMA_FRIEND_SLEEP": str(bin_dir / "sleep"),
        "CINEMA_FRIEND_VENV_DIR": str(venv),
        "CINEMA_FRIEND_ENV_FILE": str(env_file),
        "CINEMA_FRIEND_TEST_CALLS": str(calls),
    })
    return InstallerHarness(tmp_path, env, calls, venv, env_file)


def _run_function(
    installer: InstallerHarness,
    command: str,
    *,
    env: dict[str, str] | None = None,
    stdin: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "-c", 'source "$1"\n' + command, "installer-test", str(SCRIPT)],
        cwd=installer.root,
        env=env or installer.env,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_installer(
    installer: InstallerHarness,
    *,
    env: dict[str, str] | None = None,
    stdin: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        cwd=installer.root,
        env=env or installer.env,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )


def _calls(installer: InstallerHarness) -> list[str]:
    if not installer.calls.exists():
        return []
    return installer.calls.read_text(encoding="utf-8").splitlines()


def test_rejects_non_macos_systems(installer: InstallerHarness) -> None:
    env = {**installer.env, "CINEMA_FRIEND_TEST_UNAME": "Linux"}

    result = _run_function(installer, "require_supported_platform", env=env)

    assert result.returncode == 1
    assert "macOS" in result.stderr


def test_rejects_missing_launchctl(installer: InstallerHarness) -> None:
    env = {
        **installer.env,
        "CINEMA_FRIEND_LAUNCHCTL": str(installer.root / "missing-launchctl"),
    }

    result = _run_function(installer, "require_supported_platform", env=env)

    assert result.returncode == 1
    assert "launchctl is unavailable" in result.stderr


def test_rejects_python_older_than_312(installer: InstallerHarness) -> None:
    env = {**installer.env, "CINEMA_FRIEND_TEST_PYTHON_STATUS": "1"}

    result = _run_function(installer, "prepare_virtualenv", env=env)

    assert result.returncode == 1
    assert "Python 3.12 or newer is required" in result.stderr
    assert not installer.venv.exists()


def test_creates_virtualenv_and_installs_runtime_package(
    installer: InstallerHarness,
) -> None:
    result = _run_function(
        installer,
        "require_project_files\nprepare_virtualenv\ninstall_package",
    )

    assert result.returncode == 0, result.stderr
    assert (installer.venv / "bin" / "python").is_file()
    calls = _calls(installer)
    assert any(line.endswith(f"-m venv {installer.venv}") for line in calls)
    assert "python:-m pip install --upgrade pip" in calls
    assert "python:-m pip install --upgrade ." in calls
    assert all(" -e " not in line and ".[dev]" not in line for line in calls)


def test_refuses_an_incomplete_existing_virtualenv(
    installer: InstallerHarness,
) -> None:
    installer.venv.mkdir()

    result = _run_function(installer, "prepare_virtualenv")

    assert result.returncode == 1
    assert "exists but has no executable bin/python" in result.stderr
    assert installer.venv.is_dir()


def test_refuses_an_incompatible_existing_virtualenv(
    installer: InstallerHarness,
) -> None:
    created = _run_function(installer, "prepare_virtualenv")
    env = {**installer.env, "CINEMA_FRIEND_TEST_PYTHON_STATUS": "1"}

    result = _run_function(installer, "prepare_virtualenv", env=env)

    assert created.returncode == 0, created.stderr
    assert result.returncode == 1
    assert "must be Python 3.12 or newer" in result.stderr
    assert (installer.venv / "bin" / "python").is_file()


def _valid_env_text(path: Path, *, token: str = "123:abc") -> str:
    return (
        "\n".join([
            f"TELEGRAM_BOT_TOKEN={token}",
            "TELEGRAM_ALLOWED_USER_IDS=11,22",
            f"DATABASE_PATH={path.parent / 'cinema-friend.db'}",
            "LOG_LEVEL=INFO",
            "BFI_IMPERSONATE_PROFILE=chrome",
        ])
        + "\n"
    )


def test_creates_private_configuration_without_echoing_token(
    installer: InstallerHarness,
) -> None:
    token = "8012345678:AAF-secret-value"

    result = _run_function(
        installer,
        "ensure_config",
        stdin=f"{token}\n11, 22\n",
    )

    assert result.returncode == 0, result.stderr
    assert installer.env_file.read_text(encoding="utf-8") == (
        f"TELEGRAM_BOT_TOKEN={token}\n"
        "TELEGRAM_ALLOWED_USER_IDS=11, 22\n"
        "DATABASE_PATH=~/.local/state/cinema-friend/cinema-friend.db\n"
        "LOG_LEVEL=INFO\n"
        "BFI_IMPERSONATE_PROFILE=chrome\n"
    )
    assert stat.S_IMODE(installer.env_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(installer.env_file.parent.stat().st_mode) == 0o700
    assert token not in result.stdout
    assert token not in result.stderr
    assert list(installer.env_file.parent.glob(".cinema-friend.env.*")) == []


def test_reuses_existing_configuration_without_prompting(
    installer: InstallerHarness,
) -> None:
    installer.env_file.parent.mkdir(parents=True)
    original = _valid_env_text(installer.env_file)
    installer.env_file.write_text(original, encoding="utf-8")
    installer.env_file.chmod(0o600)

    result = _run_function(installer, "ensure_config")

    assert result.returncode == 0, result.stderr
    assert installer.env_file.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("stdin", "message"),
    [
        ("\n11\n", "Telegram bot token must not be empty"),
        ("123:abc\n\n", "allowed Telegram user IDs must not be empty"),
    ],
)
def test_rejects_empty_configuration_answers(
    installer: InstallerHarness,
    stdin: str,
    message: str,
) -> None:
    result = _run_function(
        installer,
        "trap cleanup_temp_config EXIT\nensure_config",
        stdin=stdin,
    )

    assert result.returncode == 1
    assert message in result.stderr
    assert not installer.env_file.exists()
    assert list(installer.env_file.parent.glob(".cinema-friend.env.*")) == []


def test_complete_install_is_idempotent(installer: InstallerHarness) -> None:
    token = "8012345678:AAF-secret-value"

    first = _run_installer(installer, stdin=f"{token}\n11,22\n")
    original = installer.env_file.read_bytes()
    second = _run_installer(installer)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert installer.env_file.read_bytes() == original
    assert token not in first.stdout
    assert token not in first.stderr
    assert "Cinema Friend is running" in first.stdout
    assert f"Configuration: {installer.env_file}" in first.stdout
    expected_log_dir = Path(installer.env["HOME"]) / "Library" / "Logs" / "cinema-friend"
    assert f"Logs: {expected_log_dir}" in first.stdout
    assert "Uninstall:" in first.stdout
    calls = _calls(installer)
    assert token not in "\n".join(calls)
    assert sum(line.endswith(f"-m venv {installer.venv}") for line in calls) == 1
    assert calls.count("python:-m pip install --upgrade .") == 2
    install_call = (
        f"python:{SCRIPT.parent / 'scripts' / 'install_launch_agent.py'} "
        f"install --env-file {installer.env_file}"
    )
    assert calls.count(install_call) == 2
    assert calls.count(f"launchctl:print gui/501/{LABEL}") == 4
    assert calls.count("sleep:1") == 2


def test_fails_after_bounded_service_verification(
    installer: InstallerHarness,
) -> None:
    installer.env_file.parent.mkdir(parents=True)
    installer.env_file.write_text(
        _valid_env_text(installer.env_file),
        encoding="utf-8",
    )
    installer.env_file.chmod(0o600)
    env = {**installer.env, "CINEMA_FRIEND_TEST_LAUNCHCTL_RUNNING": "0"}

    result = _run_installer(installer, env=env)

    assert result.returncode == 1
    assert f'launchctl print "gui/501/{LABEL}"' in result.stderr
    assert "cinema-friend.err.log" in result.stderr
    calls = _calls(installer)
    assert calls.count(f"launchctl:print gui/501/{LABEL}") == 10
    assert calls.count("sleep:1") == 9


def test_verify_service_rejects_a_one_sample_transient_running_state(
    installer: InstallerHarness,
) -> None:
    """A single healthy probe must not be enough: launchd can relaunch a crash-looping
    process fast enough that one sample lands inside a brief live window. The fake
    launchctl here reports running exactly once and never again, so success would mean
    the bounded ten-probe loop returned early on that one sample instead of requiring a
    second, one second later."""
    installer.env_file.parent.mkdir(parents=True)
    installer.env_file.write_text(
        _valid_env_text(installer.env_file),
        encoding="utf-8",
    )
    installer.env_file.chmod(0o600)
    marker = installer.root / "launchctl-running-once"
    env = {**installer.env, "CINEMA_FRIEND_TEST_LAUNCHCTL_RUNNING_ONCE": str(marker)}

    result = _run_installer(installer, env=env)

    assert result.returncode == 1
    assert f'launchctl print "gui/501/{LABEL}"' in result.stderr
    calls = _calls(installer)
    assert calls.count(f"launchctl:print gui/501/{LABEL}") == 10
    assert calls.count("sleep:1") == 9


def test_verify_service_succeeds_on_two_consecutive_healthy_samples(
    installer: InstallerHarness,
) -> None:
    result = _run_function(installer, "verify_service")

    assert result.returncode == 0, result.stderr
    calls = _calls(installer)
    assert calls.count(f"launchctl:print gui/501/{LABEL}") == 2
    assert calls.count("sleep:1") == 1


@pytest.mark.parametrize(
    "output",
    [
        "state = running",
        "pid = 4242",
        "state = waiting\npid = 4242",
        "state = running\npid = 0",
    ],
)
def test_service_requires_running_state_and_positive_pid(
    installer: InstallerHarness,
    output: str,
) -> None:
    env = {
        **installer.env,
        "CINEMA_FRIEND_TEST_LAUNCHCTL_OUTPUT": output,
    }

    result = _run_function(installer, "service_is_running", env=env)

    assert result.returncode == 1
