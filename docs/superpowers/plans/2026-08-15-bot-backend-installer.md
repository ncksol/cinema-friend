# One-command Backend Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single `./install.sh` command that turns an existing checkout into a configured, running macOS `launchd` backend.

**Architecture:** A Bash 3.2-compatible root script owns prerequisite checks, checkout-local virtual-environment setup, secure first-run prompts, and post-start verification. The existing Python configuration loader remains the source of truth for env-file validation, and the existing LaunchAgent installer remains the source of truth for plist and `launchctl` behavior.

**Tech Stack:** Bash 3.2, Python 3.12+, setuptools/pip, pytest, macOS `launchd`

## Global Constraints

- Target only single-user macOS deployments using `~/Library/LaunchAgents`.
- Require Python 3.12 or newer; do not install Python or Homebrew.
- Run from an existing checkout; do not clone or update the repository.
- Create and reuse `.venv` inside the checkout.
- Install runtime dependencies non-editably with `pip install --upgrade .`; do not install the `dev` extra.
- Store configuration at `~/.config/cinema-friend/cinema-friend.env` with mode `0600`.
- Never print the Telegram token, pass it as a process argument, or copy it into the plist.
- Reuse an existing env file unchanged and validate it before modifying the LaunchAgent.
- Do not run the live BFI contract smoke test.
- Report success only when `launchctl print` shows `state = running` and a positive PID.
- Do not add `Co-authored-by` trailers to commits.

## File Structure

- Create `install.sh`: macOS installation orchestration and operator-facing output.
- Create `tests/test_install_script.py`: subprocess coverage for shell boundaries without touching real `launchd`, Telegram, or BFI.
- Modify `scripts/install_launch_agent.py`: reuse `load_settings` for semantic env-file validation before plist or `launchctl` changes.
- Modify `tests/test_launch_agent.py`: prove invalid configuration has no deployment side effects and keep fixtures semantically valid.
- Modify `README.md`: make `./install.sh` the primary backend setup path while retaining explicit development setup and existing operational commands.

---

### Task 1: Validate Complete Configuration Before LaunchAgent Changes

**Files:**
- Modify: `scripts/install_launch_agent.py:24-126`
- Modify: `tests/test_launch_agent.py:77-82`
- Modify: `tests/test_launch_agent.py:293-344`

**Interfaces:**
- Consumes: `cinema_friend.app.load_settings(env_file: Path) -> Settings`
- Consumes: `cinema_friend.domain.errors.InputError`
- Produces: `validate_env_file(env_file: Path) -> Path`, preserving its signature while adding semantic validation
- Produces: `InstallError` with the original configuration error text and no secret values

- [ ] **Step 1: Make LaunchAgent test fixtures valid application configuration**

Add this helper above the `env_file` fixture in `tests/test_launch_agent.py`:

```python
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
```

Replace the fixture body with:

```python
@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / "cinema-friend.env"
    _write_valid_env(path)
    return path
```

In `test_install_does_not_copy_the_env_file_contents_into_the_plist`, replace the direct
one-line token write with:

```python
_write_valid_env(env_file, token="8012345678:AAF-secret-value")
```

- [ ] **Step 2: Write a failing no-side-effects test for invalid settings**

Add beside the existing install validation tests:

```python
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
```

- [ ] **Step 3: Run the new test and verify the current validator is insufficient**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_launch_agent.py::test_install_rejects_invalid_settings_before_writing_or_loading \
  -v
```

Expected: FAIL because the current permission-only validator accepts the incomplete file
and proceeds to create the plist.

- [ ] **Step 4: Delegate env-file validation to the application loader**

In `scripts/install_launch_agent.py`, remove `import stat` and add:

```python
from cinema_friend.app import load_settings
from cinema_friend.domain.errors import InputError
```

Replace `validate_env_file` with:

```python
def validate_env_file(env_file: Path) -> Path:
    """Return *env_file* absolute after validating its security and settings."""
    path = Path(env_file).expanduser().resolve()
    try:
        load_settings(path)
    except InputError as error:
        raise InstallError(str(error)) from error
    return path
```

In `resolve_executable`, update the remediation text so it matches the deployment install:

```python
raise InstallError(
    f"no {EXECUTABLE_NAME} executable beside {interpreter}; "
    "install the project into this environment first (pip install .)"
)
```

- [ ] **Step 5: Run the LaunchAgent tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_launch_agent.py -v
```

Expected: all tests PASS, including proof that invalid settings create no plist and make no
`launchctl` call.

- [ ] **Step 6: Run focused static checks**

Run:

```bash
.venv/bin/python -m ruff check scripts/install_launch_agent.py tests/test_launch_agent.py
.venv/bin/python -m mypy scripts/install_launch_agent.py
```

Expected: both commands exit 0.

- [ ] **Step 7: Commit the validation boundary**

```bash
git add scripts/install_launch_agent.py tests/test_launch_agent.py
git commit -m "fix: validate service config before launch"
```

---

### Task 2: Add the One-command macOS Installer

**Files:**
- Create: `install.sh`
- Create: `tests/test_install_script.py`

**Interfaces:**
- Consumes: `scripts/install_launch_agent.py install --env-file PATH`
- Consumes: `.venv/bin/python`
- Produces: executable `./install.sh` with no required arguments
- Produces shell functions: `require_supported_platform`, `require_project_files`, `prepare_virtualenv`, `install_package`, `ensure_config`, `install_launch_agent`, `service_is_running`, and `verify_service`
- Uses private test command overrides: `CINEMA_FRIEND_UNAME`, `CINEMA_FRIEND_LAUNCHCTL`, `CINEMA_FRIEND_ID`, `CINEMA_FRIEND_SLEEP`, `CINEMA_FRIEND_VENV_DIR`, and `CINEMA_FRIEND_ENV_FILE`

- [ ] **Step 1: Create the shell-test harness and prerequisite tests**

Create `tests/test_install_script.py` with:

```python
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
```

- [ ] **Step 2: Run the prerequisite tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_install_script.py -v
```

Expected: FAIL because `install.sh` does not exist.

- [ ] **Step 3: Implement platform, virtual-environment, and package setup**

Create `install.sh` with:

```bash
#!/bin/bash

set -euo pipefail

LABEL="com.ncksol.cinema-friend"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
VENV_DIR="${CINEMA_FRIEND_VENV_DIR:-$REPO_ROOT/.venv}"
VENV_PYTHON="$VENV_DIR/bin/python"
ENV_FILE="${CINEMA_FRIEND_ENV_FILE:-$HOME/.config/cinema-friend/cinema-friend.env}"
LOG_DIR="$HOME/Library/Logs/cinema-friend"
INSTALL_AGENT="$REPO_ROOT/scripts/install_launch_agent.py"
UNAME_BIN="${CINEMA_FRIEND_UNAME:-/usr/bin/uname}"
LAUNCHCTL_BIN="${CINEMA_FRIEND_LAUNCHCTL:-/bin/launchctl}"
ID_BIN="${CINEMA_FRIEND_ID:-/usr/bin/id}"
SLEEP_BIN="${CINEMA_FRIEND_SLEEP:-/bin/sleep}"
TEMP_ENV_FILE=""


die() {
    printf 'install error: %s\n' "$*" >&2
    exit 1
}


stage() {
    printf '==> %s\n' "$*"
}


require_supported_platform() {
    if [[ "$("$UNAME_BIN" -s)" != "Darwin" ]]; then
        die "Cinema Friend installation is supported only on macOS"
    fi
    if [[ ! -x "$LAUNCHCTL_BIN" ]]; then
        die "launchctl is unavailable at $LAUNCHCTL_BIN"
    fi
}


require_project_files() {
    [[ -f "$REPO_ROOT/pyproject.toml" ]] || die "pyproject.toml is missing from $REPO_ROOT"
    [[ -f "$INSTALL_AGENT" ]] || die "LaunchAgent installer is missing: $INSTALL_AGENT"
}


python_is_supported() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'
}


find_base_python() {
    local name
    local path
    for name in python3.12 python3; do
        path="$(command -v "$name" 2>/dev/null || true)"
        if [[ -n "$path" ]] && python_is_supported "$path"; then
            printf '%s\n' "$path"
            return 0
        fi
    done
    return 1
}


prepare_virtualenv() {
    local base_python
    if [[ -e "$VENV_DIR" || -L "$VENV_DIR" ]]; then
        [[ -x "$VENV_PYTHON" ]] || \
            die "$VENV_DIR exists but has no executable bin/python; repair or remove it"
        python_is_supported "$VENV_PYTHON" || \
            die "$VENV_PYTHON must be Python 3.12 or newer; repair or remove $VENV_DIR"
        stage "Reusing $VENV_DIR"
        return
    fi

    if ! base_python="$(find_base_python)"; then
        die "Python 3.12 or newer is required; install it and rerun ./install.sh"
    fi
    stage "Creating $VENV_DIR"
    "$base_python" -m venv "$VENV_DIR"
    [[ -x "$VENV_PYTHON" ]] || die "virtual environment did not create $VENV_PYTHON"
}


install_package() {
    stage "Upgrading pip"
    "$VENV_PYTHON" -m pip install --upgrade pip
    stage "Installing Cinema Friend runtime"
    (
        cd "$REPO_ROOT"
        "$VENV_PYTHON" -m pip install --upgrade .
    )
}


if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    die "installer orchestration is not complete"
fi
```

The temporary terminal guard deliberately prevents this intermediate version from
reporting a partial installation as success. Task 2 removes it before committing.

- [ ] **Step 4: Run the prerequisite tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_install_script.py -v
```

Expected: all prerequisite and virtual-environment tests PASS.

- [ ] **Step 5: Add secure-configuration and service-verification tests**

Append to `tests/test_install_script.py`:

```python
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
    assert calls.count(f"launchctl:print gui/501/{LABEL}") == 2


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
```

- [ ] **Step 6: Run the expanded tests and verify the missing functions fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_install_script.py -v
```

Expected: the prerequisite tests PASS; the new tests FAIL because `ensure_config` and the
complete `main` orchestration do not exist.

- [ ] **Step 7: Complete secure configuration, deployment, and verification**

Remove the temporary terminal guard from `install.sh`. Append these functions after
`install_package`:

```bash
cleanup_temp_config() {
    if [[ -n "${TEMP_ENV_FILE:-}" && -e "$TEMP_ENV_FILE" ]]; then
        rm -f "$TEMP_ENV_FILE"
    fi
}


ensure_config() {
    local config_dir
    local token
    local allowed_ids

    if [[ -e "$ENV_FILE" || -L "$ENV_FILE" ]]; then
        stage "Reusing $ENV_FILE"
        return
    fi

    config_dir="$(dirname "$ENV_FILE")"
    mkdir -p "$config_dir"
    chmod 700 "$config_dir"
    umask 077
    TEMP_ENV_FILE="$(mktemp "$config_dir/.cinema-friend.env.XXXXXX")"

    printf 'Telegram bot token: ' >&2
    if ! IFS= read -r -s token; then
        printf '\n' >&2
        die "could not read the Telegram bot token"
    fi
    printf '\n' >&2
    [[ -n "$token" ]] || die "Telegram bot token must not be empty"

    printf 'Allowed Telegram user IDs (comma-separated): ' >&2
    IFS= read -r allowed_ids || die "could not read allowed Telegram user IDs"
    [[ -n "$allowed_ids" ]] || die "allowed Telegram user IDs must not be empty"

    {
        printf 'TELEGRAM_BOT_TOKEN=%s\n' "$token"
        printf 'TELEGRAM_ALLOWED_USER_IDS=%s\n' "$allowed_ids"
        printf 'DATABASE_PATH=~/.local/state/cinema-friend/cinema-friend.db\n'
        printf 'LOG_LEVEL=INFO\n'
        printf 'BFI_IMPERSONATE_PROFILE=chrome\n'
    } > "$TEMP_ENV_FILE"
    chmod 600 "$TEMP_ENV_FILE"
    mv "$TEMP_ENV_FILE" "$ENV_FILE"
    TEMP_ENV_FILE=""
    stage "Created $ENV_FILE"
}


install_launch_agent() {
    stage "Installing and loading the LaunchAgent"
    "$VENV_PYTHON" "$INSTALL_AGENT" install --env-file "$ENV_FILE"
}


service_is_running() {
    local output
    local uid
    uid="$("$ID_BIN" -u)"
    if ! output="$("$LAUNCHCTL_BIN" print "gui/$uid/$LABEL" 2>/dev/null)"; then
        return 1
    fi
    grep -Eq '^[[:space:]]*state = running[[:space:]]*$' <<< "$output" || return 1
    grep -Eq '^[[:space:]]*pid = [1-9][0-9]*[[:space:]]*$' <<< "$output" || return 1
}


verify_service() {
    local attempt=0
    local uid
    while [[ "$attempt" -lt 10 ]]; do
        if service_is_running; then
            return
        fi
        attempt=$((attempt + 1))
        if [[ "$attempt" -lt 10 ]]; then
            "$SLEEP_BIN" 1
        fi
    done

    uid="$("$ID_BIN" -u)"
    die "service did not reach a running state; inspect: launchctl print \"gui/$uid/$LABEL\"; log: $LOG_DIR/cinema-friend.err.log"
}


main() {
    [[ "$#" -eq 0 ]] || die "usage: ./install.sh"
    trap cleanup_temp_config EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    require_supported_platform
    require_project_files
    prepare_virtualenv
    install_package
    ensure_config
    install_launch_agent
    verify_service

    cleanup_temp_config
    trap - EXIT HUP INT TERM
    printf '\nCinema Friend is running.\n'
    printf 'Configuration: %s\n' "$ENV_FILE"
    printf 'Logs: %s\n' "$LOG_DIR"
    printf 'Uninstall: "%s" "%s" uninstall\n' "$VENV_PYTHON" "$INSTALL_AGENT"
}


if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
```

Make the installer executable:

```bash
chmod 755 install.sh
```

- [ ] **Step 8: Run installer tests and shell syntax validation**

Run:

```bash
/bin/bash -n install.sh
.venv/bin/python -m pytest tests/test_install_script.py tests/test_launch_agent.py -v
```

Expected: syntax check exits 0 and all tests PASS. The tests use fake command boundaries;
they do not access the user's LaunchAgent domain or the network.

- [ ] **Step 9: Run static checks for the new tests**

Run:

```bash
.venv/bin/python -m ruff check tests/test_install_script.py
.venv/bin/python -m mypy tests/test_install_script.py
```

Expected: both commands exit 0.

- [ ] **Step 10: Commit the complete installer**

```bash
git add install.sh tests/test_install_script.py
git commit -m "feat: add one-command backend installer"
```

---

### Task 3: Make the One-command Path the Primary Documentation

**Files:**
- Modify: `README.md:13-81`
- Modify: `README.md:85-101`
- Modify: `README.md:141-175`
- Modify: `README.md:332-338`

**Interfaces:**
- Consumes: executable `./install.sh`
- Preserves: `.venv/bin/cinema-friend-smoke`
- Preserves: `.venv/bin/python scripts/install_launch_agent.py uninstall`
- Produces: one primary installation flow and a separate development setup

- [ ] **Step 1: Replace the manual production setup with the one-command flow**

Rewrite the README from `## Requirements` through the paragraph ending with
`break the fingerprint it exists to present.` to:

````markdown
## Requirements

- macOS with `launchd`
- Python 3.12 or newer
- A Telegram account

The installer checks these prerequisites but does not install system software.

---

## Before you install

### 1. Create a bot

Message [@BotFather](https://t.me/BotFather), send `/newbot`, and follow the prompts. It
replies with a token that looks like `123456789:AA...`. Anyone holding that token can act
as your bot.

### 2. Find your user ID

Message [@userinfobot](https://t.me/userinfobot). It replies with your numeric ID. Gather
the IDs of everyone who should be allowed to see and edit watches.

---

## Install

From an existing checkout:

```sh
./install.sh
```

On the first run, the installer asks for the bot token without echoing it and then asks for
the comma-separated allowed user IDs. It creates:

- `.venv` in the checkout, containing the runtime installation;
- `~/.config/cinema-friend/cinema-friend.env`, mode `0600`;
- `~/Library/LaunchAgents/com.ncksol.cinema-friend.plist`;
- `~/Library/Logs/cinema-friend/`.

It loads the LaunchAgent and exits successfully only after `launchd` reports a running
process. Rerun the same command after updating the checkout. It reuses `.venv` and the
existing configuration, reinstalls the checkout's current code, and reloads the service.

The installer never replaces an existing configuration. If that file is invalid, it stops
before changing the LaunchAgent and reports the validation error.
````

- [ ] **Step 2: Align the smoke-test section with post-install use**

Rename `## Check the BFI contract before you deploy` to:

```markdown
## Check the BFI contract
```

Replace its opening paragraph with:

```markdown
Cinema Friend reads a public page that BFI never promised to keep stable. After installation
and whenever BFI behavior is in doubt, confirm that the page still looks the way the parsers
expect:
```

Keep the existing command, exit-code table, and explanation unchanged.

- [ ] **Step 3: Replace the manual deploy section with installation details**

Rename `## Deploy` to:

```markdown
## Installation details
```

Replace its introductory manual-install command, option table, and validation paragraph
with:

````markdown
`install.sh` invokes the existing LaunchAgent installer with the checkout's virtual
environment:

```sh
.venv/bin/python scripts/install_launch_agent.py install \
  --env-file ~/.config/cinema-friend/cinema-friend.env
```

That lower-level command remains available for service-only reinstalls. It validates the
complete configuration before writing the plist, creates the log directory, and replaces a
previously loaded copy rather than failing.
````

Keep `### Confirm it is running`, `### One copy at a time`, `### Logs`, and
`### Stop, start, remove` as the operational reference.

- [ ] **Step 4: Add explicit development setup**

At the start of `## Development`, before the existing test commands, add:

````markdown
Production installation intentionally excludes development tools. For a development
checkout:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
```

Then run:
````

Keep the existing pytest, Ruff, and mypy command block after that sentence.

- [ ] **Step 5: Review every installation command for consistency**

Run:

```bash
rg -n "pip install|install_launch_agent|install\\.sh|before you deploy|## Deploy" README.md
```

Expected:

- `./install.sh` is the primary production path.
- `pip install -e '.[dev]'` appears only in Development.
- the lower-level LaunchAgent command remains in installation details, uninstall, and
  backup/restore operations where it is intentional.
- `before you deploy` and `## Deploy` have no matches.

- [ ] **Step 6: Run the complete implementation checks**

Run:

```bash
/bin/bash -n install.sh
.venv/bin/python -m pytest tests/test_install_script.py tests/test_launch_agent.py -v
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m mypy src scripts
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 7: Commit the documentation**

```bash
git add README.md
git commit -m "docs: document one-command backend install"
```
